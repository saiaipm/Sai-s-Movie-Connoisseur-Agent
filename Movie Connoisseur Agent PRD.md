# **Product Requirement Document (PRD): "Movie Connoisseur" AI Agent**

**Document Version:** 1.1 — revised 2026-07-29 after Phases 1–3 were built and
verified against the live APIs. Changes from v1.0 are listed in §8; the
corrections are also applied inline below.

**Target Platform:** Google Agent Development Kit (ADK)

**Primary UI/Deployment Target:** Streamlit (Hosted on Streamlit Community Cloud)

**Data Storage:** Google Sheets API

**External Data Source:** The Movie Database (TMDB) API

## **1\. Executive Summary & Objectives**

The **Movie Connoisseur** is a conversational, multi-agent AI assistant designed to simplify movie discovery across Indian OTT platforms, provide rich film metadata and critical insights, and enable users to maintain a personal movie journal directly connected to a Google Sheet.

### **Key Objectives**

* **Real-time India OTT Discovery:** Filter and search movies available on major streaming platforms in India (Netflix, Prime Video, Disney+ Hotstar, JioCinema, Zee5, etc.).  
* **Deep Metadata Retrieval:** Deliver detailed runtime, genre, director, cast, synopsis, and rating information for any title.  
* **Persistent Journaling:** Log watched movies, personal ratings, and notes into a user-owned Google Sheet database.  
* **Shareable Insights:** Format movie logs and watchlists into clean summaries ready to share with friends on messaging apps or social media.

## **2\. System Architecture**

The system utilizes a **Multi-Agent Router Architecture** implemented via Google ADK. A central **Coordinator Agent** analyzes incoming user prompts and delegates execution to specialized sub-agents.

                            ┌───────────────────────────┐  
                            │    User / Streamlit UI    │  
                            └─────────────┬─────────────┘  
                                          │  
                            ┌─────────────▼─────────────┐  
                            │     Coordinator Agent     │  
                            │   (Intent Router/Planner) │  
                            └─────────────┬─────────────┘  
                                          │  
      ┌───────────────────────────────────┼───────────────────────────────────┐  
      │                                   │                                   │  
┌─────▼───────────────┐         ┌─────────▼───────────┐         ┌─────────────▼───────────┐  
│   Discovery Agent   │         │    Critic Agent     │         │      Journal Agent      │  
│                     │         │                     │         │                         │  
│ • TMDB Discover Tool│         │ • TMDB Details Tool │         │ • Read Sheets Tool      │  
│ • Regional Filters  │         │ • Cast & Runtime    │         │ • Write Sheets Tool     │  
│   (IN OTT Providers)│         │ • Plot & Ratings    │         │ • Share/Export Tool     │  
└─────────┬───────────┘         └─────────┬───────────┘         └─────────────┬───────────┘  
          │                               │                                   │  
          ▼                               ▼                                   ▼  
┌─────────────────────┐         ┌─────────────────────┐         ┌─────────────────────────┐  
│   TMDB REST API     │         │   TMDB REST API     │         │   Google Sheets API     │  
│  (Watch Providers)  │         │   (/movie/{id})     │         │       (gspread)         │  
└─────────────────────┘         └─────────────────────┘         └─────────────────────────┘

## **3\. Agent Roles & Specifications**

### **3.1 Coordinator Agent (Router)**

* **Model:** configurable — default **gemini-3.1-flash-lite**. (v1.0 specified
  gemini-2.5-flash, which Google has since retired: it returns `404 NOT_FOUND`
  on new API keys. See §8.)  
* **Function:** Serves as the primary entry point. Parses intent from user inputs and routes tasks to the appropriate specialized agent.  
* **System Prompt Strategy:** Instructed to recognize three main intent categories:  
  1. *Searching / Discovering content* ![][image1] Route to **Discovery Agent**  
  2. *Asking about specific movie info/ratings/cast* ![][image1] Route to **Critic Agent**  
  3. *Logging, reading, or sharing movie logs* ![][image1] Route to **Journal Agent**

### **3.2 Discovery Agent**

* **Purpose:** Searches for movies based on OTT platform availability in India, genre, release year, or language.  
* **Tools:**  
  * fetch\_ott\_movies(provider\_id, genre\_id, release\_year)  
* **Supported Indian Provider IDs (TMDB)** — verified against TMDB's live India
  list on 2026-07-29:  
  * Netflix: 8  
  * Amazon Prime Video: 119  
  * **JioHotstar: 2336** *(replaces Disney+ Hotstar 122 and JioCinema 220, which
    merged in 2025; both old IDs now return zero results in India)*  
  * Zee5: 232  
  * SonyLIV: 237  
  * Sun NXT: 309, Apple TV: 350, ManoramaMax: 482, MX Player: 515, aha: 532

  The retired names ("Hotstar", "Disney+ Hotstar", "JioCinema") remain accepted
  as user input and resolve to 2336.

### **3.3 Critic & Detail Agent**

* **Purpose:** Provides complete details on specific movies, helps users decide what to watch, and offers contextual recommendations.  
* **Tools:**  
  * fetch\_movie\_details(movie\_id\_or\_title)  
  * fetch\_movie\_credits(movie\_id)  
* **Key Outputs:** Plot summary, director, top cast members, runtime (formatted in hours/minutes), age rating, and average community rating.

### **3.4 Journal Agent**

* **Purpose:** Manages the user's movie diary on Google Sheets.  
* **Tools:**  
  * add\_to\_journal(title, platform, rating, review, watch\_date)  
  * get\_journal\_history(limit, filter\_rating)  
  * generate\_shareable\_summary(movie\_ids)  
* **Data Engine:** Interacts with Google Sheets using the Python gspread library and a Service Account JSON key.

## **4\. Google Sheets Database Schema**

The agent reads and writes to a single Google Sheet workbook containing a worksheet named **Movie\_Journal**.

| Column | Field Name | Data Type | Example Value | Description |
| :---- | :---- | :---- | :---- | :---- |
| **A** | Log\_ID | String / UUID | LOG-9821 | Unique key for the entry |
| **B** | Watch\_Date | Date (YYYY-MM-DD) | 2026-07-28 | Date the user watched the movie |
| **C** | Movie\_Title | String | Maharaja | Official title of the movie |
| **D** | TMDB\_ID | Integer | 109123 | ID used for fetching poster/metadata |
| **E** | OTT\_Platform | String | Netflix | Streaming platform used |
| **F** | Genre | String | Action, Thriller | Primary genres |
| **G** | User\_Rating | Float (1.0 \- 5.0) | 4.5 | Personal star rating given by user |
| **H** | User\_Review | Text | Brilliant screenplay and twist\! | Personal notes or mini-review |
| **I** | Shared\_Status | Boolean | TRUE | Whether this entry was exported/shared |

### **4.1 Watchlist Worksheet (added in v1.1)**

A second worksheet named **Watchlist** in the same workbook holds films the user
intends to watch. Kept separate from Movie\_Journal because "want to watch" and
"have watched" have different lifecycles.

| Column | Field Name | Data Type | Example Value | Description |
| :---- | :---- | :---- | :---- | :---- |
| **A** | Watchlist\_ID | String | WL-3F9A2C71 | Unique key for the entry |
| **B** | Added\_Date | Date (YYYY-MM-DD) | 2026-07-29 | When it was saved |
| **C** | Movie\_Title | String | Maharaja | Official title |
| **D** | TMDB\_ID | Integer | 1118224 | For poster/metadata lookups |
| **E** | OTT\_Platform | String | Netflix | Where it currently streams in India |
| **F** | Genre | String | Action, Thriller | Primary genres |
| **G** | Notes | Text | Vijay Sethupathi thriller | Why they want to watch it |

**Tools:** add\_to\_watchlist(title, notes), get\_watchlist(limit),
remove\_from\_watchlist(title).

**Rules:**

* Adding is **confirm-first** — the agent searches TMDB, presents the match with
  its release year, and writes only after the user agrees. This doubles as
  disambiguation for shared titles.
* Adding is idempotent; a title already saved is reported, not duplicated.
* Removal is permanent, so an ambiguous title returns candidates instead of
  deleting a row.
* Logging a film via add\_to\_journal removes it from the watchlist
  automatically; a failure there never fails the log.

## **5\. Functional Workflows & Sample Dialogue**

### **Workflow A: Discovery & Inquiry**

> **User:** *"What are the top thriller movies available on Netflix India right now?"*

> **Coordinator:** Routes request to Discovery Agent.

> **Discovery Agent:** Invokes fetch\_ott\_movies(provider\_id="8", genre="Thriller").

> **Response:** Displays top 5 matches with ratings and short descriptions.

### **Workflow B: Logging & Journaling**

> **User:** *"I just finished watching Stree 2 on Hotstar. Give it 4 out of 5 stars and log it: 'Super funny, great performance by Rajkummar\!'"*

> **Coordinator:** Routes request to Journal Agent.

> **Journal Agent:** Extracts metadata, fetches TMDB ID for accuracy, and executes add\_to\_journal().

> **Response:** *"Done\! I've added 'Stree 2' to your Google Sheet with a 4.0/5 rating."*

### **Workflow C: Sharing Watch History**

> **User:** *"Give me a summary of my last 3 watched movies formatted nicely so I can text it to my friends."*

> **Coordinator:** Routes request to Journal Agent.

> **Journal Agent:** Calls get\_journal\_history(limit=3) and formats a Markdown/WhatsApp-friendly card.

> **Response:**

> 🍿 My Recent Movie Logs:  
> 1\. Stree 2 (Disney+ Hotstar) \- ⭐️ 4.0/5  
>    "Super funny, great performance by Rajkummar\!"  
> 2\. Maharaja (Netflix) \- ⭐️ 4.5/5  
>    "Brilliant screenplay and twist\!"  
> 3\. Kalki 2898 AD (Prime Video) \- ⭐️ 3.5/5  
>    "Great visual effects and world-building."

## **6\. Technical Requirements & Dependencies**

### **Python Libraries Required**

* google-adk: Core agent SDK.  
* google-genai: Gemini API client.  
* gspread: Google Sheets API integration.  
* oauth2client / google-auth: Authentication for Google Cloud Service Account.  
* requests: Calling TMDB API endpoints.  
* streamlit: Building the frontend UI.

### **Credentials Needed**

1. GEMINI\_API\_KEY / GOOGLE\_API\_KEY: From Google AI Studio.  
2. TMDB\_API\_KEY: TMDB v3 API Key.  
3. GOOGLE\_SERVICE\_ACCOUNT\_JSON: Service Account credentials for Google Sheets access.  
4. SPREADSHEET\_KEY: ID of the target Google Sheet document.

Optional, for alternative model providers: OPENAI\_API\_KEY, NVIDIA\_API\_KEY.

## **7\. Implementation Roadmap**

1. **Phase 1: Tool Construction (Colab)**  
   * Write and test fetch\_ott\_movies and fetch\_movie\_details with TMDB API.  
   * Set up Google Service Account and test gspread append/read functions.  
2. **Phase 2: Multi-Agent Assembly (ADK)**  
   * Define DiscoveryAgent, CriticAgent, and JournalAgent.  
   * Configure the CoordinatorAgent router rules in Python.  
3. **Phase 3: Frontend Integration**  
   * Wrap the ADK runner inside a Streamlit application (app.py).  
   * Create interactive UI elements (chat interface, Google Sheet view tab).  
4. **Phase 4: Deployment**  
   * Push code to a private GitHub repository.  
   * Deploy to **Streamlit Community Cloud** with secret environment variables attached.  
   * Note: Streamlit Community Cloud installs from `requirements.txt`, not from
     `pyproject.toml`/uv, and its apps are **publicly reachable with no
     authentication** — see §8 on quota exposure.

## **8\. Revision Log — v1.0 to v1.1**

Each item below was found by running the implementation against the live APIs,
not by review.

| # | v1.0 assumption | Verified reality | Resolution |
| :---- | :---- | :---- | :---- |
| 1 | Disney+ Hotstar = 122, JioCinema = 220 | Both retired; merged into **JioHotstar 2336**. Old IDs return zero results in India. | §3.2 updated; old names kept as aliases |
| 2 | Model `gemini-2.5-flash` | Returns `404 NOT_FOUND` — withdrawn for new API keys | Default is now `gemini-3.1-flash-lite`, configurable |
| 3 | Model choice is a fixed constant | Gemini free tier allows only **5 req/min** for non-lite flash; one user turn costs 2–3 calls, so it throttles immediately. Lite tier allows 15/min. | Lite model default; provider is now pluggable across Gemini / OpenAI / NVIDIA NIM |
| 4 | `google-adk` 1.x | Installs at **2.x**; different runner and session API | Implementation follows the 2.x API |
| 5 | TMDB reachable at `api.themoviedb.org` | Several Indian ISPs reset TLS to that host | Automatic failover to `api.tmdb.org`, TMDB's own alias |
| 6 | `fetch_ott_movies(provider_id, genre_id, …)`, but §5 sample dialogue calls it with `genre="Thriller"` | Both forms needed | Tools accept a name **or** an ID for provider and genre |
| 7 | Deployment target implies controlled access | Streamlit Community Cloud apps are public and unauthenticated — any visitor spends the owner's API quota | Added `MAX_MESSAGES_PER_SESSION` cap |

**Not changed:** the multi-agent router architecture (§2), the agent
responsibilities (§3), the Google Sheets schema (§4), and all three functional
workflows (§5) were implemented as specified and verified working.

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABUAAAAZCAYAAADe1WXtAAAAeUlEQVR4XmNgGAWjYOCBvLx8K7oYxUBBQcFDSUmJH12cYgB07UVFRUV5dHGKANDQWUC8B10cDoCS06CKSMJycnILgPQvIO5DN5M2hpIDxMXFuYGGLZaWlpZBlyMbAA28QtWIAiUnoKFB6OIUAXkaJX4FdLFRMApoCADLri0q8MCj7gAAAABJRU5ErkJggg==>