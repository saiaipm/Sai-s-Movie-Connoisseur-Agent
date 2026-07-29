"""Movie Connoisseur — Streamlit frontend.

    uv run streamlit run app.py

Two tabs: a chat with the agent tree, and a live view of the Google Sheet
journal. The conversation lives in st.session_state so it survives Streamlit's
rerun-on-every-interaction model.
"""

from __future__ import annotations

import streamlit as st

from movie_connoisseur import config
from movie_connoisseur.agents import missing_credentials
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

EXAMPLE_PROMPTS = [
    "What thrillers are on Netflix India right now?",
    "Tell me about Maharaja",
    "Any good Tamil movies on Zee5?",
    *(
        []
        if config.DEMO_MODE
        else [
            "I watched Stree 2 on JioHotstar, 4 stars — log it",
            "Add Maharaja to my watchlist",
        ]
    ),
    "Summarise my last 3 watches so I can text them",
]

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


# --- State -----------------------------------------------------------------


def get_chat() -> MovieChat:
    """The conversation for this browser session, created once."""
    if "chat" not in st.session_state:
        st.session_state.chat = MovieChat(user_id="streamlit_user")
    return st.session_state.chat


def reset_chat() -> None:
    old = st.session_state.pop("chat", None)
    if old is not None:
        old.close()
    st.session_state.messages = []


if "messages" not in st.session_state:
    st.session_state.messages = []


# --- Sidebar ---------------------------------------------------------------


def render_sidebar() -> None:
    with st.sidebar:
        st.title("🍿 Movie Connoisseur")
        st.caption("Discover, research and journal movies on Indian OTT.")

        if config.DEMO_MODE:
            st.info(
                "**Read-only demo.** Browse and ask anything about films. The "
                "journal is the owner's real diary and cannot be written to."
            )

        missing = missing_credentials()
        if missing:
            st.error("Missing credentials:\n\n" + "\n".join(f"- `{m}`" for m in missing))
            st.caption("Add them to `.env` (local) or app secrets (Streamlit Cloud).")
        else:
            st.success("All credentials configured")

        st.divider()
        st.subheader("Try asking")
        for prompt in EXAMPLE_PROMPTS:
            if st.button(prompt, use_container_width=True, key=f"eg_{prompt[:20]}"):
                st.session_state.pending_prompt = prompt
                st.rerun()

        st.divider()
        if st.button("New conversation", use_container_width=True):
            reset_chat()
            st.rerun()

        if config.MAX_MESSAGES_PER_SESSION:
            used = sum(1 for m in st.session_state.messages if m["role"] == "user")
            st.caption(
                f"Demo mode — {used}/{config.MAX_MESSAGES_PER_SESSION} messages used"
            )

        with st.expander("Configuration"):
            st.write(f"**Provider:** {config.MODEL_PROVIDER}")
            if config.PROVIDER_WAS_FORCED:
                st.caption(
                    "Demo mode forced the free provider, overriding the "
                    "configured one."
                )
            st.write(f"**Model:** `{config.MODEL_NAME}`")
            st.write(f"**Region:** {config.WATCH_REGION}")
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


def answer(prompt: str) -> None:
    """Send one message and append both sides to the transcript."""
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            turn = get_chat().send(prompt)

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


def render_chat_tab() -> None:
    # The sidebar is collapsed by default on mobile, so the read-only notice
    # has to live in the main pane too or phone visitors never see it.
    if config.DEMO_MODE:
        st.warning(
            "🔒 **Read-only demo.** Ask anything about films and browse the "
            "journal, but nothing can be saved — the journal and watchlist "
            "belong to the app's owner."
        )

    if not st.session_state.messages:
        st.info(
            "Ask me what's streaming in India or dig into a specific film."
            if config.DEMO_MODE
            else "Ask me what's streaming in India, dig into a specific film, or "
            "log what you watched. Try one of the examples in the sidebar."
        )

    # The transcript renders into a container declared before the input, so a
    # reply being streamed appears above the box rather than below it.
    transcript = st.container()
    with transcript:
        for message in st.session_state.messages:
            render_message(message)

    cap = config.MAX_MESSAGES_PER_SESSION
    used = sum(1 for m in st.session_state.messages if m["role"] == "user")
    if cap and used >= cap:
        st.session_state.pop("pending_prompt", None)
        st.warning(
            f"Demo limit reached ({cap} messages per session). Start a new "
            "conversation from the sidebar, or run the app locally with your "
            "own API key for unlimited use."
        )
        return

    pending = st.session_state.pop("pending_prompt", None)
    typed = st.chat_input("Ask about movies, or log what you watched…")

    prompt = pending or typed
    if prompt:
        with transcript:
            answer(prompt)
        st.rerun()


# --- Journal tab -----------------------------------------------------------


def render_journal_tab() -> None:
    left, right = st.columns([3, 1])
    left.subheader("Movie journal" if config.DEMO_MODE else "Your movie journal")
    if right.button("Refresh", use_container_width=True):
        journal.reset_connection()
        st.rerun()

    if config.DEMO_MODE:
        st.caption("🔒 Read-only — this is the app owner's journal.")

    if config.SPREADSHEET_KEY and not config.DEMO_MODE:
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
            "Nothing logged yet."
            if config.DEMO_MODE
            else "Nothing logged yet. Tell the agent what you watched and it'll appear here."
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


def render_watchlist_tab() -> None:
    left, right = st.columns([3, 1])
    left.subheader("Watchlist" if config.DEMO_MODE else "Your watchlist")
    if right.button("Refresh", use_container_width=True, key="refresh_watchlist"):
        journal.reset_connection()
        st.rerun()

    st.caption(
        "🔒 Read-only — this is the app owner's watchlist."
        if config.DEMO_MODE
        else "Films saved to watch later. Logging one as watched removes it from here."
    )

    with st.spinner("Reading your sheet…"):
        result = journal.get_watchlist(limit=100)

    if result["status"] == "error":
        st.error(result["error_message"])
        return

    entries = result["entries"]
    if not entries:
        st.info(
            "Nothing saved yet."
            if config.DEMO_MODE
            else 'Nothing saved yet. Try "add Maharaja to my watchlist" in the chat.'
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

render_sidebar()

if missing_credentials():
    st.title("Movie Connoisseur")
    st.warning("Add the missing credentials listed in the sidebar, then reload.")
    st.stop()

chat_tab, journal_tab, watchlist_tab = st.tabs(
    ["💬 Chat", "📓 Journal", "🔖 Watchlist"]
)

with chat_tab:
    render_chat_tab()

with journal_tab:
    render_journal_tab()

with watchlist_tab:
    render_watchlist_tab()
