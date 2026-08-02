"""Sai's Streaming Companion — Streamlit frontend.

    uv run streamlit run app.py

Two tabs: a chat with the agent tree, and a live view of the Google Sheet
journal. The conversation lives in st.session_state so it survives Streamlit's
rerun-on-every-interaction model.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from movie_connoisseur import config
from movie_connoisseur.agents import (
    MODEL_ERROR,
    build_agent_tree,
    build_model,
    missing_credentials,
)
from movie_connoisseur.chat import MovieChat
from movie_connoisseur.tools import journal, tmdb

st.set_page_config(
    page_title="Sai's Streaming Companion",
    page_icon="🍿",
    layout="wide",
    initial_sidebar_state="expanded",
)

AGENT_LABELS = {
    "discovery_agent": "🔍 Discovery",
    "critic_agent": "🎬 Critic",
    "journal_agent": "📓 Journal",
    "movie_connoisseur": "🍿 Connoisseur",
}

def example_prompts(write_enabled: bool) -> list[str]:
    """Suggested prompts — only offer the write ones if they would work."""
    prompts = [
        "What thrillers are on Netflix India right now?",
        "Tell me about Maharaja",
        "Any good Tamil movies on Zee5?",
    ]
    if write_enabled:
        prompts += [
            "I watched Stree 2 on JioHotstar, 4 stars — log it",
            "Add Maharaja to my watchlist",
        ]
    prompts.append("Summarise my last 3 watches so I can text them")
    return prompts

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.5rem; max-width: 1100px; }
      /* Agent attribution chip above each reply */
      .agent-chip {
        display: inline-block; font-size: 0.72rem; font-weight: 600;
        letter-spacing: 0.03em; padding: 0.12rem 0.55rem; border-radius: 999px;
        background: rgba(128,128,128,0.15); margin-bottom: 0.4rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- Identity & permission -------------------------------------------------


def auth_configured() -> bool:
    """Whether an [auth] block exists, i.e. sign-in is available."""
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def signed_in_email() -> str:
    """Email of the signed-in user, or empty if nobody is signed in."""
    try:
        user = st.user
        if getattr(user, "is_logged_in", False):
            return str(user.get("email") or "")
    except Exception:
        pass
    return ""


def message_cap(write_enabled: bool) -> int:
    """Per-session message cap. The owner is not rate-limited on their own app."""
    return 0 if write_enabled else config.MAX_MESSAGES_PER_SESSION


def resolve_provider(trusted: bool) -> str:
    """Which model provider to use for this session — see config for the rule."""
    return config.resolve_session_provider(
        trusted, st.session_state.get("provider", "")
    )


def resolve_permission() -> tuple[bool, str, bool]:
    """Decide whether *this visitor* may write.

    Two independent routes to write access:
      - the deployment itself allows it (WRITE_ENABLED — local development)
      - the visitor signed in as the configured owner

    Anonymous visitors on a public deployment get neither, which is the point.

    Returns:
        (write_enabled, signed_in_email, is_owner)
    """
    email = signed_in_email()
    is_owner = bool(
        email and config.OWNER_EMAIL and email.lower() == config.OWNER_EMAIL.lower()
    )
    return (config.WRITE_ENABLED or is_owner, email, is_owner)


# --- State -----------------------------------------------------------------


def get_chat(write_enabled: bool, provider: str) -> MovieChat:
    """The conversation for this browser session.

    Rebuilt if write permission or the provider changes: the agent's toolset,
    instructions and model all differ, so signing in or switching provider must
    not leave the previous tree in place.
    """
    signature = (write_enabled, provider)
    if st.session_state.get("chat_signature") != signature:
        reset_chat()
        st.session_state.chat_signature = signature

    if "chat" not in st.session_state:
        st.session_state.chat = MovieChat(
            user_id="streamlit_user",
            write_enabled=write_enabled,
            agent=build_agent_tree(
                write_enabled=write_enabled,
                model=build_model(provider, config.default_model_for(provider)),
            ),
        )
    return st.session_state.chat


def reset_chat() -> None:
    old = st.session_state.pop("chat", None)
    if old is not None:
        old.close()
    st.session_state.messages = []


if "messages" not in st.session_state:
    st.session_state.messages = []


# --- Sidebar ---------------------------------------------------------------


def render_account(write_enabled: bool, email: str, is_owner: bool) -> None:
    """Sign-in controls and what the current visitor is allowed to do."""
    if not auth_configured():
        # No [auth] block: permission comes purely from WRITE_ENABLED.
        if not write_enabled:
            st.info(
                "**Read-only.** Browse and ask anything about films. The journal "
                "and watchlist belong to the app's owner."
            )
        return

    if email:
        # Keyed to identity, not permission: locally WRITE_ENABLED can be true
        # for a non-owner, and calling them "owner" would be wrong.
        if is_owner:
            st.success(f"Signed in as owner\n\n`{email}`")
        else:
            st.info(
                f"Signed in as `{email}` — read-only. Only the owner's account "
                "can change the journal."
            )
        if st.button("Sign out", use_container_width=True):
            st.logout()
    else:
        # Nobody signed in. Locally WRITE_ENABLED can still be true, so do not
        # claim read-only without checking — the message would contradict what
        # the app actually allows.
        if write_enabled:
            st.success("Full access (enabled for this deployment)")
        else:
            st.info(
                "**Read-only.** Ask anything about films and browse the journal. "
                "The owner can sign in for full access."
            )
        if st.button("Sign in with Google", use_container_width=True):
            st.login("google")


def render_provider_picker() -> None:
    """Model provider picker, shown only to trusted sessions.

    A visitor must never be able to select a billed provider.
    config.resolve_session_provider() enforces that independently of whether
    this widget is rendered, so the guarantee does not rest on the UI.
    """
    options = config.available_providers()
    if len(options) < 2:
        st.caption(
            f"Only one provider has a key configured (`{options[0] if options else 'none'}`). "
            "Add OPENAI_API_KEY or GEMINI_API_KEY to switch."
        )
        return

    current = st.session_state.get("provider", config.MODEL_PROVIDER)
    if current not in options:
        current = options[0]

    st.selectbox(
        "Model provider",
        options,
        index=options.index(current),
        format_func=lambda p: config.PROVIDER_LABELS.get(p, p),
        key="provider",
        help="Only you can change this. Visitors always use the free provider.",
    )
    if st.session_state.get("provider") == "openai":
        st.caption("⚠️ OpenAI is billed per request.")


def render_sidebar(
    write_enabled: bool, email: str, is_owner: bool, provider: str
) -> None:
    with st.sidebar:
        st.title("🍿 Sai's Streaming Companion")
        st.caption("Discover, research and journal movies on Indian OTT.")

        render_account(write_enabled, email, is_owner)

        missing = missing_credentials()
        if missing:
            st.error("Missing credentials:\n\n" + "\n".join(f"- `{m}`" for m in missing))
            st.caption("Add them to `.env` (local) or app secrets (Streamlit Cloud).")
        elif MODEL_ERROR:
            st.error(f"Model not available:\n\n{MODEL_ERROR}")
        else:
            st.success("All credentials configured")

        st.divider()
        st.subheader("Try asking")
        for prompt in example_prompts(write_enabled):
            if st.button(prompt, use_container_width=True, key=f"eg_{prompt[:20]}"):
                st.session_state.pending_prompt = prompt
                st.rerun()

        st.divider()
        if st.button("New conversation", use_container_width=True):
            reset_chat()
            st.rerun()

        cap = message_cap(write_enabled)
        if cap:
            used = sum(1 for m in st.session_state.messages if m["role"] == "user")
            st.caption(f"{used}/{cap} messages used this session")

        if write_enabled:
            render_provider_picker()

        with st.expander("Configuration"):
            st.write(f"**Provider:** {provider}")
            if not is_owner and config.PROVIDER_WAS_FORCED:
                st.caption(
                    "The free provider was forced, overriding the configured one."
                )
            st.write(f"**Model:** `{config.default_model_for(provider)}`")
            st.write(f"**Region:** {config.WATCH_REGION}")
            st.write(f"**Write access:** {'yes' if write_enabled else 'read-only'}")
            # Absent OMDb degrades silently — ratings columns just stay empty —
            # so it has to be visible somewhere.
            if config.OMDB_API_KEY:
                st.write("**Critic ratings:** OMDb configured")
            else:
                st.warning(
                    "OMDB_API_KEY not set — IMDb, Rotten Tomatoes and "
                    "Metacritic will be blank on anything logged from here.",
                    icon="⚠️",
                )
            host = tmdb.active_host() or "not yet contacted"
            st.write(f"**TMDB host:** `{host}`")


# --- Chat tab --------------------------------------------------------------


def render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant" and message.get("agent"):
            label = AGENT_LABELS.get(message["agent"], message["agent"])
            st.markdown(f'<span class="agent-chip">{label}</span>', unsafe_allow_html=True)
        st.markdown(message["content"])

        if message.get("tools"):
            with st.expander("Tool calls", expanded=False):
                for call in message["tools"]:
                    icon = "✅" if call["status"] == "success" else "⚠️"
                    st.markdown(f"{icon} `{call['name']}`")
                    if call["args"]:
                        st.json(call["args"], expanded=False)
                    if call["error"]:
                        st.caption(call["error"])


def answer(prompt: str, write_enabled: bool, provider: str) -> None:
    """Send one message and append both sides to the transcript."""
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            turn = get_chat(write_enabled, provider).send(prompt)

        if turn.agent:
            label = AGENT_LABELS.get(turn.agent, turn.agent)
            st.markdown(f'<span class="agent-chip">{label}</span>', unsafe_allow_html=True)
        st.markdown(turn.text)

        if turn.retry_after:
            st.warning(f"Rate limited — wait about {turn.retry_after}s before retrying.")

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": turn.text,
            "agent": turn.agent,
            "tools": [
                {
                    "name": c.name,
                    "args": c.args,
                    "status": c.status,
                    "error": c.error,
                }
                for c in turn.tool_calls
            ],
        }
    )


def render_chat_tab(write_enabled: bool, provider: str) -> None:
    # The sidebar is collapsed by default on mobile, so the read-only notice
    # has to live in the main pane too or phone visitors never see it.
    if not write_enabled:
        st.warning(
            "🔒 **Read-only.** Ask anything about films and browse the journal, "
            "but nothing can be saved — the journal and watchlist belong to the "
            "app's owner."
        )

    if not st.session_state.messages:
        st.info(
            "Ask me what's streaming in India, dig into a specific film, or log "
            "what you watched. Try one of the examples in the sidebar."
            if write_enabled
            else "Ask me what's streaming in India or dig into a specific film."
        )

    # The transcript renders into a container declared before the input, so a
    # reply being streamed appears above the box rather than below it.
    transcript = st.container()
    with transcript:
        for message in st.session_state.messages:
            render_message(message)

    cap = message_cap(write_enabled)
    used = sum(1 for m in st.session_state.messages if m["role"] == "user")
    if cap and used >= cap:
        st.session_state.pop("pending_prompt", None)
        st.warning(
            f"Session limit reached ({cap} messages). Start a new conversation "
            "from the sidebar, or run the app locally with your own API key for "
            "unlimited use."
        )
        return

    pending = st.session_state.pop("pending_prompt", None)
    typed = st.chat_input("Ask about movies, or log what you watched…")

    prompt = pending or typed
    if prompt:
        with transcript:
            answer(prompt, write_enabled, provider)
        st.rerun()


# --- Shared table helpers --------------------------------------------------


def _number(value) -> float | None:
    """Blank cells to None so a rating column stays numeric.

    A mix of numbers and empty strings makes the column object-dtype, and
    NumberColumn then refuses to format it.
    """
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_frame(rows: list[dict], numeric: list[str]) -> pd.DataFrame:
    """Build the table with genuinely numeric rating columns.

    Coercing keeps the columns sortable and lets NumberColumn format them; a
    column of mixed numbers and empty strings would be object dtype, which
    NumberColumn refuses.

    Missing values still show as a grey "None" — that is Streamlit's own
    placeholder for a null and there is no column_config option to change it.
    Rendering blanks instead would mean formatting these as strings, which
    would cost the sorting the columns exist for.
    """
    frame = pd.DataFrame(rows)
    for column in numeric:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


NUMERIC_COLUMNS = ["Yours", "TMDB", "IMDb", "RT", "MC"]


# Shared by both tables: same four sources, same scales, same formatting.
RATING_COLUMNS = {
    "TMDB": st.column_config.NumberColumn(
        "TMDB", format="%.1f", help="TMDB community score, out of 10", width="small"
    ),
    "IMDb": st.column_config.NumberColumn(
        "IMDb", format="%.1f", help="IMDb user score, out of 10", width="small"
    ),
    "RT": st.column_config.NumberColumn(
        "RT", format="%d%%", help="Rotten Tomatoes, out of 100", width="small"
    ),
    "MC": st.column_config.NumberColumn(
        "MC",
        format="%d",
        help="Metacritic, out of 100. Absent for most Indian films.",
        width="small",
    ),
    "Synopsis": st.column_config.TextColumn(
        "Synopsis", help="Click a cell to read the full text", width="medium"
    ),
}


def _rating_cells(entry: dict) -> dict:
    return {
        "TMDB": _number(entry.get("tmdb_rating")),
        "IMDb": _number(entry.get("imdb_rating")),
        "RT": _number(entry.get("rt_rating")),
        "MC": _number(entry.get("metacritic")),
    }


# --- Journal statistics ----------------------------------------------------

# A single bar implies a pattern that is not there, so below three entries the
# breakdowns are withheld. Above that they are shown but labelled provisional
# until the sample is worth trusting — hiding them entirely just made the
# dashboard look broken, which is a worse failure than an honest caveat.
MIN_FOR_BREAKDOWN = 3
CONFIDENT_SAMPLE = 10
MIN_FOR_CADENCE = 2  # distinct months


def render_journal_stats(entries: list[dict], total: int) -> None:
    stats = journal.summarise_entries(entries)

    series = stats["media_types"].get("tv", 0)
    films = total - series

    top_genre = stats["genres"][0] if stats["genres"] else None
    top_platform = stats["platforms"][0] if stats["platforms"] else None
    taste = stats["taste"]

    a, b, c, d = st.columns(4)
    a.metric(
        "Titles logged",
        total,
        help=f"{films} film(s), {series} series" if series else None,
    )
    b.metric(
        "Top genre",
        top_genre[0] if top_genre else "—",
        help=f"{top_genre[1]} of {total} titles" if top_genre else None,
    )
    c.metric(
        "Most watched on",
        top_platform[0] if top_platform else "—",
        help=f"{top_platform[1]} of {total} titles" if top_platform else None,
    )
    if taste:
        d.metric(
            "You vs critics",
            f"{taste['yours']}/10",
            delta=f"{taste['delta']:+.1f} vs IMDb",
            help=(
                f"Your average rating doubled to a /10 scale, against IMDb for "
                f"the same {taste['sample']} title(s) you rated."
            ),
        )
    else:
        d.metric("You vs critics", "—", help="Rate some titles to compare.")

    if total < MIN_FOR_BREAKDOWN:
        st.caption(
            f"Log {MIN_FOR_BREAKDOWN} titles and the genre and platform "
            "breakdowns appear here."
        )
        return

    if total < CONFIDENT_SAMPLE:
        st.caption(
            f"⚠️ Based on {total} titles — treat these as provisional. Patterns "
            f"get meaningful somewhere past {CONFIDENT_SAMPLE}."
        )

    left, right = st.columns(2)
    with left:
        st.caption("**Genres** — a title counts towards each of its genres")
        st.bar_chart(
            pd.DataFrame(stats["genres"][:8], columns=["Genre", "Titles"]).set_index(
                "Genre"
            ),
            horizontal=True,
        )
    with right:
        st.caption("**Platforms**")
        st.bar_chart(
            pd.DataFrame(stats["platforms"], columns=["Platform", "Titles"]).set_index(
                "Platform"
            ),
            horizontal=True,
        )

    if len(stats["months"]) >= MIN_FOR_CADENCE:
        st.caption("**Titles per month**")
        st.bar_chart(
            pd.DataFrame(stats["months"], columns=["Month", "Titles"]).set_index("Month")
        )


# --- Journal tab -----------------------------------------------------------


def render_journal_tab(write_enabled: bool) -> None:
    left, right = st.columns([3, 1])
    left.subheader("Your journal" if write_enabled else "Journal")
    if right.button("Refresh", use_container_width=True):
        journal.reset_connection()
        st.rerun()

    if not write_enabled:
        st.caption("🔒 Read-only — this is the app owner's journal.")

    if config.SPREADSHEET_KEY and write_enabled:
        st.caption(
            f"[Open in Google Sheets]"
            f"(https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_KEY})"
        )

    with st.spinner("Reading your sheet…"):
        result = journal.get_journal_history(limit=100)

    if result["status"] == "error":
        st.error(result["error_message"])
        return

    entries = result["entries"]
    if not entries:
        st.info(
            "Nothing logged yet. Tell the agent what you watched and it will appear here."
            if write_enabled
            else "Nothing logged yet."
        )
        return

    render_journal_stats(entries, result["total_logged"])

    st.dataframe(
        _numeric_frame(
            [
                {
                    "Date": e["watch_date"],
                    "Title": e["title"],
                    "Platform": e["platform"],
                    "Genre": e["genre"],
                    "Yours": e["rating"] or None,
                    **_rating_cells(e),
                    "Review": e["review"],
                    "Synopsis": e["synopsis"],
                    "Shared": e["shared"],
                }
                for e in entries
            ],
            NUMERIC_COLUMNS,
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Yours": st.column_config.NumberColumn(
                "Yours",
                format="%.1f ⭐",
                min_value=0,
                max_value=5,
                help="Your own rating, out of 5",
                width="small",
            ),
            **RATING_COLUMNS,
            "Review": st.column_config.TextColumn(width="medium"),
            "Shared": st.column_config.CheckboxColumn(width="small"),
        },
    )
    st.caption(
        "Your rating is out of 5; TMDB and IMDb out of 10; RT and Metacritic "
        "out of 100. \"None\" means that source has no score for the film."
    )


# --- Watchlist tab ---------------------------------------------------------


def render_watchlist_tab(write_enabled: bool) -> None:
    left, right = st.columns([3, 1])
    left.subheader("Your watchlist" if write_enabled else "Watchlist")
    if right.button("Refresh", use_container_width=True, key="refresh_watchlist"):
        journal.reset_connection()
        st.rerun()

    st.caption(
        "Films saved to watch later. Logging one as watched removes it from here."
        if write_enabled
        else "🔒 Read-only — this is the app owner's watchlist."
    )

    with st.spinner("Reading your sheet…"):
        result = journal.get_watchlist(limit=100)

    if result["status"] == "error":
        st.error(result["error_message"])
        return

    entries = result["entries"]
    if not entries:
        st.info(
            'Nothing saved yet. Try "add Maharaja to my watchlist" in the chat.'
            if write_enabled
            else "Nothing saved yet."
        )
        return

    rated = [_number(e.get("imdb_rating")) for e in entries]
    rated = [r for r in rated if r is not None]

    a, b = st.columns(2)
    a.metric("Films saved", result["total"])
    b.metric(
        "Average IMDb", f"{sum(rated) / len(rated):.1f}/10" if rated else "—"
    )

    st.dataframe(
        _numeric_frame(
            [
                {
                    "Added": e["added_date"],
                    "Title": e["title"],
                    "Platform": e["platform"],
                    "Genre": e["genre"],
                    **_rating_cells(e),
                    "Notes": e["notes"],
                    "Synopsis": e["synopsis"],
                }
                for e in entries
            ],
            NUMERIC_COLUMNS,
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            **RATING_COLUMNS,
            "Notes": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption(
        "TMDB and IMDb are out of 10; RT and Metacritic out of 100. \"None\" means "
        "that source has no score for the film."
    )


# --- Main ------------------------------------------------------------------

# Resolved once per rerun and threaded through, so every part of the page agrees
# on what this visitor may do.
WRITE_ENABLED, USER_EMAIL, IS_OWNER = resolve_permission()
PROVIDER = resolve_provider(WRITE_ENABLED)

render_sidebar(WRITE_ENABLED, USER_EMAIL, IS_OWNER, PROVIDER)

if missing_credentials() or MODEL_ERROR:
    st.title("🍿 Sai's Streaming Companion")
    st.warning(
        "This app is not fully configured yet — see the sidebar for what is "
        "missing, add it to the app's secrets, and reload."
    )
    st.caption(
        "On Streamlit Cloud: Manage app → Settings → Secrets. "
        "Locally: the `.env` file in the project root."
    )
    st.stop()

chat_tab, journal_tab, watchlist_tab = st.tabs(
    ["💬 Chat", "📓 Journal", "🔖 Watchlist"]
)

with chat_tab:
    render_chat_tab(WRITE_ENABLED, PROVIDER)

with journal_tab:
    render_journal_tab(WRITE_ENABLED)

with watchlist_tab:
    render_watchlist_tab(WRITE_ENABLED)
