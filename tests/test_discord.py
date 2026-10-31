"""Discord delivery: the HTML->Markdown edge, and the request that goes out.

No network. A `httpx.MockTransport` stands in for Discord and records the
request, so these pin the *contract* with the REST API — endpoint, auth header,
payload shape, rate-limit handling — not anything about a real server. They do
not prove a bot is invited with the right permissions; the first live send does.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerts  # noqa: E402
import config  # noqa: E402
from alerts import Discord, html_to_markdown, split_title  # noqa: E402
from test_alerts import DEAL, make_item  # noqa: E402


class Unpriced:
    """A bare keyword match: no reference price behind it."""

    priced = False
    qualifies = True
    reason = "matches search"
    is_seed = False
    n_comps = 0


# ----------------------------------------------------------- html_to_markdown
def test_bold_italic_and_strikethrough_map_to_discord_markdown():
    out = html_to_markdown("Precio: <b>180€</b> <i>(n=12)</i> (antes <s>210€</s>)")
    assert out == "Precio: **180€** *(n=12)* (antes ~~210€~~)"


def test_seller_text_cannot_become_formatting():
    """A listing titled "RTX_3070 *URGE* [oferta]" is data, not markup."""
    out = html_to_markdown("RTX_3070 *URGE* [oferta] #1 ~nuevo~ `x` ||spoiler|| > cita")
    assert out == r"RTX\_3070 \*URGE\* \[oferta\] \#1 \~nuevo\~ \`x\` \|\|spoiler\|\| \> cita"


def test_mention_syntax_in_seller_text_is_defanged():
    """"<@123>" would render as a user mention. allowed_mentions stops the ping;
    escaping stops the rendering."""
    assert html_to_markdown("&lt;@123456&gt; hola") == r"\<@123456\> hola"


def test_entities_are_decoded_before_escaping():
    assert html_to_markdown("Tom &amp; Jerry") == "Tom & Jerry"


def test_an_escaped_tag_in_seller_text_is_not_mistaken_for_ours():
    """build_caption html.escapes seller text, so a literal "<b>" in a title
    reaches here as &lt;b&gt; and must stay text — only tags we wrote format."""
    assert html_to_markdown("&lt;b&gt;hola&lt;/b&gt;") == r"\<b\>hola\</b\>"


def test_newlines_stay_plain():
    """Discord, unlike CommonMark, treats a single newline as a line break."""
    assert html_to_markdown("uno\ndos") == "uno\ndos"


def test_a_url_only_line_is_left_unescaped():
    """Escaping the underscores would corrupt the link."""
    url = "https://es.wallapop.com/item/rtx_3070-gb-1?a=1&amp;b=2"
    assert html_to_markdown(url) == "https://es.wallapop.com/item/rtx_3070-gb-1?a=1&b=2"


def test_pre_becomes_a_fenced_block_with_contents_untouched():
    out = html_to_markdown("<pre>Traceback:\n  a &lt; b &amp;&amp; c_d *e*</pre>")
    assert out == "```\nTraceback:\n  a < b && c_d *e*\n```"


def test_the_fence_outgrows_any_backtick_run_inside_the_code():
    out = html_to_markdown("<pre>x ``` y</pre>")
    assert out.startswith("````\n") and out.endswith("\n````")


# ---------------------------------------------------------------- split_title
def test_first_line_becomes_a_plain_text_title():
    title, body = split_title("<b>CHOLLO</b> · RTX 3070 &amp; co\nPrecio: <b>1€</b>")
    assert title == "CHOLLO · RTX 3070 & co"
    assert body == "Precio: <b>1€</b>"


def test_error_text_splits_into_title_and_a_code_block():
    title, body = split_title(alerts.build_error_text("boom"))
    assert title == "wallapop-bot"
    assert body.startswith("<pre>") and "boom" in body


# --------------------------------------------------------------- the request
@pytest.fixture
def server(monkeypatch):
    """A Discord whose HTTP client is a MockTransport. `.requests` records every
    request; set `.handler` to change the response; `.slept` records waits."""
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DISCORD_RATE_DELAY", 0.0)

    class Server:
        def __init__(self):
            self.requests = []
            self.slept = []
            self.handler = lambda request: httpx.Response(200, json={"id": "1"})

    state = Server()
    monkeypatch.setattr(alerts.time, "sleep", lambda s: state.slept.append(s))

    def build(token="BOTTOKEN.abc.def", channel_id="123456789012345678"):
        d = Discord(token=token, channel_id=channel_id)
        d._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (state.requests.append(request), state.handler(request))[1]
            )
        )
        return d

    state.build = build
    return state


def _json(request):
    return json.loads(request.content)


def _embed(server, n=0):
    return _json(server.requests[n])["embeds"][0]


def test_alert_posts_to_the_channel_with_the_bot_token_in_a_header(server):
    assert server.build().send_alert(make_item(), DEAL, "new", model_display="RTX 4070") is True

    (req,) = server.requests
    assert req.method == "POST"
    assert req.url.path == "/api/v10/channels/123456789012345678/messages"
    assert req.headers["Authorization"] == "Bot BOTTOKEN.abc.def"
    # httpx logs the full URL at INFO, so the token must never be in it.
    assert "BOTTOKEN" not in str(req.url)


def test_alert_embed_has_title_description_link_image_and_colour(server):
    server.build().send_alert(make_item(), DEAL, "new", model_display="RTX 4070")
    embed = _embed(server)

    assert embed["title"] == "CHOLLO · RTX 4070"
    assert "**210€**" in embed["description"]
    assert embed["url"] == "https://es.wallapop.com/item/rtx-4070-gigabyte-123"
    assert embed["image"] == {"url": "https://cdn/x.jpg"}
    assert embed["color"] == alerts.COLOR_DEAL


def test_the_trailing_url_line_is_dropped_because_the_title_links_to_it(server):
    server.build().send_alert(make_item(), DEAL, "new")
    assert "wallapop.com" not in _embed(server)["description"]


def test_a_rejected_image_url_retries_without_the_picture(server):
    """Found by sending a real one: a malformed image URL is not a picture that
    fails to load, it is a 400 on the whole message — and without a fallback that
    loses the alert."""
    def reject_images(request):
        if "image" in _json(request)["embeds"][0]:
            return httpx.Response(400, json={"code": 50035, "message": "Invalid Form Body"})
        return httpx.Response(200, json={"id": "1"})

    server.handler = reject_images
    assert server.build().send_alert(make_item(), DEAL, "new") is True

    assert len(server.requests) == 2
    assert "image" in _embed(server, 0)
    assert "image" not in _embed(server, 1)


def test_a_400_with_no_image_is_not_retried(server):
    """The fallback is for the image; anything else a 400 means is a real fault."""
    server.handler = lambda request: httpx.Response(400, json={"code": 50035})
    assert server.build().send_alert(make_item(image_url=None), DEAL, "new") is False
    assert len(server.requests) == 1


def test_an_outage_does_not_trigger_the_image_fallback(server):
    """Only a 400 means "this body was rejected"; a 500 would just double the
    requests against a server that is already struggling."""
    server.handler = lambda request: httpx.Response(500, text="boom")
    assert server.build().send_alert(make_item(), DEAL, "new") is False
    assert len(server.requests) == 1


def test_no_image_means_no_image_field(server):
    server.build().send_alert(make_item(image_url=None), DEAL, "new")
    assert "image" not in _embed(server)


def test_mentions_are_disabled_whatever_the_seller_wrote(server):
    """A listing titled "@everyone" must be text, never a ping."""
    server.build().send_alert(make_item(title="@everyone RTX 4070 URGE"), DEAL, "new")
    assert _json(server.requests[0])["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize(
    "deal,kind,colour",
    [
        (DEAL, "new", alerts.COLOR_DEAL),
        (DEAL, "price_drop", alerts.COLOR_DROP),
        (Unpriced(), "new", alerts.COLOR_MATCH),
    ],
)
def test_colour_tells_a_deal_from_a_drop_from_a_bare_match(server, deal, kind, colour):
    server.build().send_alert(make_item(), deal, kind, previous_price=250.0)
    assert _embed(server)["color"] == colour


def test_error_ping_is_a_red_embed_with_a_code_block(server):
    assert server.build().send_error("boom: it broke") is True
    embed = _embed(server)
    assert embed["title"] == "wallapop-bot"
    assert embed["color"] == alerts.COLOR_ERROR
    assert "```" in embed["description"] and "boom: it broke" in embed["description"]


def test_overlong_text_is_cut_to_discords_limits_instead_of_losing_the_alert(server):
    """Over the limit is a 400, and a 400 loses the whole message."""
    d = server.build()
    embed = d._embed("t" * 1000, "d" * 9000, alerts.COLOR_DEAL)
    assert len(embed["title"]) == alerts.EMBED_TITLE_LIMIT
    assert len(embed["description"]) == alerts.EMBED_DESCRIPTION_LIMIT


# ----------------------------------------------------------------- rate limits
def test_a_short_rate_limit_is_waited_out_and_retried(server):
    responses = iter([
        httpx.Response(429, json={"retry_after": 0.8, "global": False}),
        httpx.Response(200, json={"id": "1"}),
    ])
    server.handler = lambda request: next(responses)

    assert server.build().send_error("x") is True
    assert len(server.requests) == 2
    assert server.slept == [0.8]


def test_a_long_rate_limit_is_not_waited_out(server):
    """Blocking a loop run for a minute is worse than skipping: an unsent alert is
    not recorded, so the next cycle retries it anyway."""
    server.handler = lambda request: httpx.Response(429, json={"retry_after": 60.0})
    assert server.build().send_error("x") is False
    assert len(server.requests) == 1
    assert server.slept == []


def test_persistent_rate_limiting_gives_up_after_a_bounded_number_of_attempts(server):
    server.handler = lambda request: httpx.Response(429, json={"retry_after": 0.1})
    assert server.build().send_error("x") is False
    assert len(server.requests) == alerts.SEND_ATTEMPTS


def test_a_429_without_a_readable_retry_after_is_a_failure_not_a_crash(server):
    server.handler = lambda request: httpx.Response(429, text="not json")
    assert server.build().send_error("x") is False


# ------------------------------------------------------------------- failures
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_an_error_response_is_reported_as_not_delivered(server, status):
    server.handler = lambda request: httpx.Response(status, json={"message": "nope", "code": 50013})
    d = server.build()
    assert d.send_alert(make_item(), DEAL, "new") is False
    assert d.send_error("x") is False


def test_an_unreachable_api_is_reported_not_raised(server):
    def refuse(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    server.handler = refuse
    d = server.build()
    assert d.send_alert(make_item(), DEAL, "new") is False
    assert d.send_error("x") is False


@pytest.mark.parametrize(
    "token,channel_id",
    [
        ("", "123456789012345678"),                  # token missing
        ("BOTTOKEN", ""),                            # channel missing
        ("BOTTOKEN", "alertas"),                     # a channel *name*
        ("BOTTOKEN", "https://discord.com/channels/1/2"),  # a pasted URL
    ],
)
def test_a_misconfigured_notifier_fails_loudly_without_touching_the_network(server, token, channel_id):
    d = server.build(token=token, channel_id=channel_id)
    assert d.send_error("x") is False
    assert d.send_alert(make_item(), DEAL, "new") is False
    assert server.requests == []


def test_whitespace_around_the_secrets_is_stripped(server):
    """A secret pasted into a prompt routinely carries a trailing newline."""
    server.build(token=" BOTTOKEN\n", channel_id=" 123456789012345678\n").send_error("x")
    assert server.requests[0].headers["Authorization"] == "Bot BOTTOKEN"
    assert server.requests[0].url.path.endswith("/123456789012345678/messages")


def test_dry_run_sends_nothing_and_reports_success(server, monkeypatch):
    """The loops depend on this: a dry-run that returned False would count every
    alert as a failed send, and one that sent would not be a dry run."""
    monkeypatch.setattr(config, "DRY_RUN", True)
    d = server.build()
    assert d.send_alert(make_item(), DEAL, "new") is True
    assert d.send_error("x") is True
    assert server.requests == []
