"""Movie Connoisseur — Streamlit frontend.

    uv run streamlit run app.py

Two tabs: a chat with the agent tree, and a live view of the Google Sheet
journal. The conversation lives in st.session_state so it survives Streamlit's
rerun-on-every-interaction model.
"""

from __future__ import annotations

import streamlit as st

from movie_connoisseur import config
from movie_connoisseur.agents import MODEL_ERROR, build_agent_tree, missing_credentials
from movie_connoisseur.chat import MovieChat
from movie_connoisseur.tools import journal, tmdb

st.set_page_config(
    page_title="Movie Connoisseur",
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


def get_chat(write_enabled: bool) -> MovieChat:
    """The conversation for this browser session.

    Rebuilt if write permission changes, because the agent's toolset and
    instructions differ — signing in must not leave a read-only tree in place.
    """
    if st.session_state.get("chat_write_enabled") != write_enabled:
        reset_chat()
        st.session_state.chat_write_enabled = write_enabled

    if "chat" not in st.session_state:
        st.session_state.chat = MovieChat(
            user_id="streamlit_user",
            write_enabled=write_enabled,
            agent=build_agent_tree(write_enabled=write_enabled),
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
        st.info(
            "**Read-only.** Ask anything about films and browse the journal. "
            "The owner can sign in for full access."
        )
        if st.button("Sign in with Google", use_container_width=True):
            st.login("google")


def render_sidebar(write_enabled: bool, email: str, is_owner: bool) -> None:
    with st.sidebar:
        st.title("🍿 Movie Connoisseur")
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

        with st.expander("Configuration"):
            st.write(f"**Provider:** {config.MODEL_PROVIDER}")
            if config.PROVIDER_WAS_FORCED:
                st.caption(
                    "The free provider was forced, overriding the configured one."
                )
            st.write(f"**Model:** `{config.MODEL_NAME}`")
            st.write(f"**Region:** {config.WATCH_REGION}")
            st.write(f"**Write access:** {'yes' if write_enabled else 'read-only'}")
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


def answer(prompt: str, write_enabled: bool) -> None:
    """Send one message and append both sides to the transcript."""
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            turn = get_chat(write_enabled).send(prompt)

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


def render_chat_tab(write_enabled: bool) -> None:
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
            answer(prompt, write_enabled)
        st.rerun()


# --- Journal tab -----------------------------------------------------------


def render_journal_tab(write_enabled: bool) -> None:
    left, right = st.columns([3, 1])
    left.subheader("Your movie journal" if write_enabled else "Movie journal")
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
            "Nothing logged yet. Tell the agent what you watched and it'll appear here."
            if write_enabled
            else "Nothing logged yet."
        )
        return

    rated = [e["rating"] for e in entries if e["rating"]]
    platforms = [e["platform"] for e in entries if e["platform"]]

    a, b, c = st.columns(3)
    a.metric("Movies logged", result["total_logged"])
    b.metric("Average rating", f"{sum(rated) / len(rated):.1f}/5" if rated else "—")
    c.metric(
        "Most watched on",
        max(set(platforms), key=platforms.count) if platforms else "—",
    )

    st.dataframe(
        [
            {
                "Date": e["watch_date"],
                "Title": e["title"],
                "Platform": e["platform"],
                "Genre": e["genre"],
                "Rating": e["rating"] or None,
                "Review": e["review"],
                "Shared": e["shared"],
            }
            for e in entries
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rating": st.column_config.NumberColumn(format="%.1f ⭐", min_value=0, max_value=5),
            "Shared": st.column_config.CheckboxColumn(),
            "Review": st.column_config.TextColumn(width="large"),
        },
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

    st.metric("Films saved", result["total"])
    st.dataframe(
        [
            {
                "Added": e["added_date"],
                "Title": e["title"],
                "Platform": e["platform"],
                "Genre": e["genre"],
                "Notes": e["notes"],
            }
            for e in entries
        ],
        use_container_width=True,
        hide_index=True,
        column_config={"Notes": st.column_config.TextColumn(width="large")},
    )


# --- Main ------------------------------------------------------------------

# Resolved once per rerun and threaded through, so every part of the page agrees
# on what this visitor may do.
WRITE_ENABLED, USER_EMAIL, IS_OWNER = resolve_permission()

render_sidebar(WRITE_ENABLED, USER_EMAIL, IS_OWNER)

if missing_credentials() or MODEL_ERROR:
    st.title("🍿 Movie Connoisseur")
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
    render_chat_tab(WRITE_ENABLED)

with journal_tab:
    render_journal_tab(WRITE_ENABLED)

with watchlist_tab:
    render_watchlist_tab(WRITE_ENABLED)
