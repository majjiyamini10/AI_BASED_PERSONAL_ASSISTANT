"""
AI-Based Personal Assistant - Single File Version

Run:
    streamlit run app.py

Install:
    pip install streamlit google-genai python-dotenv SpeechRecognition gTTS

.env:
    GEMINI_API_KEY=your_api_key_here
"""

import io
import os
import re
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import streamlit as st


# ============================================================
# OPTIONAL .ENV SUPPORT
# ============================================================

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = "assistant.db"
GEMINI_MODEL = "gemini-3.8-flash"

st.set_page_config(
    page_title="AI Personal Assistant",
    page_icon="🤖",
    layout="wide",
)


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    """Create a SQLite database connection."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create required database tables."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                due TEXT NOT NULL,
                done INTEGER DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                done INTEGER DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prefs (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def set_pref(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO prefs(key, value) VALUES(?, ?)",
            (key, value),
        )


def get_pref(key, default=""):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM prefs WHERE key = ?",
            (key,),
        ).fetchone()

    return row[0] if row else default


# ============================================================
# REMINDERS
# ============================================================

def add_reminder(text, due):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO reminders(text, due, done) VALUES(?, ?, 0)",
            (
                text,
                due.isoformat(timespec="minutes"),
            ),
        )


def list_reminders():
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, text, due
            FROM reminders
            WHERE done = 0
            ORDER BY due
            """
        ).fetchall()


def due_reminders():
    """
    Return reminders that are due now and mark them as completed.
    """
    now = datetime.now().isoformat(timespec="minutes")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, text
            FROM reminders
            WHERE done = 0 AND due <= ?
            """,
            (now,),
        ).fetchall()

        for reminder_id, _ in rows:
            conn.execute(
                "UPDATE reminders SET done = 1 WHERE id = ?",
                (reminder_id,),
            )

    return [text for _, text in rows]


# ============================================================
# TASKS
# ============================================================

def add_task(text):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks(text, done) VALUES(?, 0)",
            (text,),
        )


def list_tasks():
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, text
            FROM tasks
            WHERE done = 0
            ORDER BY id
            """
        ).fetchall()


def complete_task(task_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET done = 1 WHERE id = ?",
            (task_id,),
        )


# ============================================================
# TIME PARSER
# ============================================================

def parse_time(text):
    """
    Understand commands such as:

        Remind me to study in 10 minutes
        Remind me to call mom in 2 hours
        Remind me to submit project at 6 PM
        Remind me to attend meeting tomorrow at 10 AM

    Returns:
        (datetime or None, cleaned task text)
    """

    original = text
    lower_text = text.lower()
    now = datetime.now()
    due = None

    # --------------------------------------------------------
    # "in 10 minutes" / "in 2 hours"
    # --------------------------------------------------------

    match = re.search(
        r"\bin\s+(\d+)\s*(minute|min|minutes|hour|hr|hours)\b",
        lower_text,
    )

    if match:
        number = int(match.group(1))
        unit = match.group(2)

        if unit.startswith("hour") or unit.startswith("hr"):
            due = now + timedelta(hours=number)
        else:
            due = now + timedelta(minutes=number)

        lower_text = lower_text.replace(match.group(0), "")

    else:
        # ----------------------------------------------------
        # "at 6 PM", "at 18:30", etc.
        # ----------------------------------------------------

        match = re.search(
            r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*"
            r"(am|pm|a\.m\.|p\.m\.)?\b",
            lower_text,
        )

        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)

            am_pm = (match.group(3) or "").replace(".", "")

            if am_pm == "pm" and hour < 12:
                hour += 12

            if am_pm == "am" and hour == 12:
                hour = 0

            if hour > 23 or minute > 59:
                return None, original

            due = now.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )

            # If "tomorrow" is present, always move to tomorrow.
            if "tomorrow" in lower_text:
                due += timedelta(days=1)

            # If time has already passed, assume tomorrow.
            elif due <= now:
                due += timedelta(days=1)

            lower_text = lower_text.replace(match.group(0), "")

        elif "tomorrow" in lower_text:
            # "remind me tomorrow to submit..."
            due = now + timedelta(days=1)
            due = due.replace(second=0, microsecond=0)

    # --------------------------------------------------------
    # Clean command text
    # --------------------------------------------------------

    task = re.sub(
        r"\b(remind me to|remind me|set a reminder to|please)\b",
        "",
        lower_text,
        flags=re.IGNORECASE,
    )

    task = re.sub(
        r"\btomorrow\b",
        "",
        task,
        flags=re.IGNORECASE,
    )

    task = re.sub(r"\s+", " ", task).strip(" ,.")

    return due, task


# ============================================================
# GEMINI AI
# ============================================================

def ask_gemini(prompt, history=None):
    """
    Send a user prompt to Gemini.
    """

    api_key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )

    if not api_key:
        return (
            "⚠️ Gemini API key is missing.\n\n"
            "Create a .env file in the same folder as app.py and add:\n\n"
            "GEMINI_API_KEY=your_api_key_here"
        )

    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        name = get_pref("name")

        context = (
            "You are a friendly and helpful AI personal assistant. "
            "Give concise and useful answers. "
            f"The user's name is {name or 'unknown'}.\n"
        )

        if history:
            recent_history = history[-6:]

            context += "\nRecent conversation:\n"

            for role, message in recent_history:
                context += f"{role}: {message}\n"

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=context + "\nUser: " + prompt,
        )

        if response and response.text:
            return response.text

        return "I couldn't generate a response."

    except Exception as error:
        return f"⚠️ Gemini error: {error}"


# ============================================================
# AUTOMATION
# ============================================================

APPS = {
    "notepad": "notepad",
    "calculator": "calc",
    "paint": "mspaint",
    "command prompt": "cmd",
    "file explorer": "explorer",
    "vs code": "code",
}


SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
}


def open_app(name):
    """
    Open an application or website.
    """

    name = name.strip().lower()

    # Websites
    if name in SITES:
        webbrowser.open(SITES[name])
        return f"🌐 Opening {name} in your browser."

    # Applications
    if name in APPS:
        try:
            command = APPS[name]

            if sys.platform.startswith("win"):
                subprocess.Popen(command, shell=True)

            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", command])

            else:
                subprocess.Popen([command])

            return f"🚀 Opening {name}."

        except Exception as error:
            return f"❌ Couldn't open {name}: {error}"

    return f"❓ I don't know how to open '{name}' yet."


def web_search(query):
    """
    Open Google search in browser.
    """

    query = query.strip()

    if not query:
        return "Please tell me what you want to search for."

    url = (
        "https://www.google.com/search?q="
        + quote_plus(query)
    )

    webbrowser.open(url)

    return f"🔎 Searching the web for: {query}"


# ============================================================
# COMMAND PROCESSOR
# ============================================================

def process_command(text, history=None):

    text = text.strip()

    if not text:
        return "Please enter a command."

    low = text.lower()

    # --------------------------------------------------------
    # Save user's name
    # --------------------------------------------------------

    match = re.search(
        r"\bmy name is ([a-zA-Z]+)\b",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        name = match.group(1).title()

        set_pref("name", name)

        return f"Nice to meet you, {name}! I'll remember that. 😊"

    # --------------------------------------------------------
    # Reminders
    # --------------------------------------------------------

    if "remind" in low or "set a reminder" in low:

        due, task = parse_time(text)

        if not due:
            return (
                "⏰ Please specify a time.\n\n"
                "Example:\n"
                "'Remind me to submit my project at 6 PM'\n\n"
                "or\n\n"
                "'Remind me to study in 30 minutes'"
            )

        if not task:
            task = "Reminder"

        add_reminder(task, due)

        return (
            f"⏰ Reminder set!\n\n"
            f"**Task:** {task}\n\n"
            f"**Time:** {due.strftime('%d %b %Y, %I:%M %p')}"
        )

    # --------------------------------------------------------
    # Add task
    # --------------------------------------------------------

    if low.startswith(
        (
            "add task",
            "add todo",
            "add to-do",
            "add to do",
        )
    ):

        task = re.sub(
            r"^add\s+(task|todo|to-do|to do)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" :,-")

        if not task:
            return "Please tell me what task you want to add."

        add_task(task)

        return f"✅ Added to your to-do list: {task}"

    # --------------------------------------------------------
    # Show tasks
    # --------------------------------------------------------

    if (
        "show tasks" in low
        or "my tasks" in low
        or "todo list" in low
        or "to-do list" in low
        or low in ("tasks", "todos")
    ):

        rows = list_tasks()

        if not rows:
            return "📝 Your to-do list is empty."

        result = "📝 **Your To-Do List:**\n\n"

        for index, (_, task) in enumerate(rows, start=1):
            result += f"{index}. {task}\n"

        return result

    # --------------------------------------------------------
    # Show reminders
    # --------------------------------------------------------

    if (
        "show reminders" in low
        or "my reminders" in low
        or low in ("reminders", "reminder list")
    ):

        rows = list_reminders()

        if not rows:
            return "⏰ You have no pending reminders."

        result = "⏰ **Pending Reminders:**\n\n"

        for _, reminder, due in rows:
            try:
                due_date = datetime.fromisoformat(due)
                formatted = due_date.strftime(
                    "%d %b %Y, %I:%M %p"
                )
            except ValueError:
                formatted = due

            result += f"- {reminder} — {formatted}\n"

        return result

    # --------------------------------------------------------
    # Current time
    # --------------------------------------------------------

    if (
        ("what time" in low)
        or ("current time" in low)
        or low == "time"
    ):

        return (
            "🕐 It's "
            + datetime.now().strftime("%I:%M %p")
            + "."
        )

    # --------------------------------------------------------
    # Current date
    # --------------------------------------------------------

    if (
        ("what date" in low)
        or ("today's date" in low)
        or low == "date"
        or low == "today"
    ):

        return (
            "📅 Today is "
            + datetime.now().strftime("%A, %d %B %Y")
            + "."
        )

    # --------------------------------------------------------
    # Open website/application
    # --------------------------------------------------------

    if low.startswith("open "):

        name = text[5:].strip()

        if not name:
            return "Please tell me what you want to open."

        return open_app(name)

    # --------------------------------------------------------
    # Search web
    # --------------------------------------------------------

    if low.startswith("search ") or low.startswith("google "):

        query = re.sub(
            r"^(search|google)(\s+for)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        return web_search(query)

    # --------------------------------------------------------
    # Everything else -> Gemini
    # --------------------------------------------------------

    return ask_gemini(text, history)


# ============================================================
# SPEECH TO TEXT
# ============================================================

def speech_to_text(audio_bytes):
    """
    Convert WAV audio into text.
    """

    try:
        import speech_recognition as sr

        recognizer = sr.Recognizer()

        with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
            audio = recognizer.record(source)

        return recognizer.recognize_google(audio)

    except Exception as error:
        return f"[Could not understand audio: {error}]"


# ============================================================
# TEXT TO SPEECH
# ============================================================

def text_to_speech(text):
    """
    Convert response text to MP3.
    """

    try:
        from gtts import gTTS

        buffer = io.BytesIO()

        clean_text = re.sub(
            r"[*_#`]",
            "",
            text,
        )

        gTTS(
            text=clean_text[:500],
            lang="en",
        ).write_to_fp(buffer)

        return buffer.getvalue()

    except Exception:
        return None


# ============================================================
# INITIALIZE DATABASE
# ============================================================

init_db()


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown(
    """
    <style>

    .stApp {
        background:
        linear-gradient(
            135deg,
            #0f0c29,
            #302b63,
            #24243e
        );

        color: #eeeeee;
    }

    section[data-testid="stSidebar"] {
        background:
        rgba(255, 255, 255, 0.06);

        backdrop-filter: blur(12px);

        border-right:
        1px solid rgba(255, 255, 255, 0.1);
    }

    .stChatMessage {
        background:
        rgba(255, 255, 255, 0.07);

        border:
        1px solid rgba(255, 255, 255, 0.12);

        border-radius: 16px;

        padding: 10px;
    }

    h1, h2, h3 {
        color: white;
    }

    .stButton > button {
        background:
        rgba(255, 255, 255, 0.1);

        color: white;

        border-radius: 10px;

        border:
        1px solid rgba(255, 255, 255, 0.2);
    }

    .stButton > button:hover {
        background:
        rgba(124, 92, 255, 0.5);
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# SESSION STATE
# ============================================================

if "history" not in st.session_state:
    st.session_state.history = []

if "last_audio" not in st.session_state:
    st.session_state.last_audio = None

if "speak" not in st.session_state:
    st.session_state.speak = True

if "seen_audio" not in st.session_state:
    st.session_state.seen_audio = None


# ============================================================
# HANDLE COMMAND
# ============================================================

def handle(user_text):

    reply = process_command(
        user_text,
        st.session_state.history,
    )

    st.session_state.history.append(
        ("user", user_text)
    )

    st.session_state.history.append(
        ("assistant", reply)
    )

    if st.session_state.speak:
        st.session_state.last_audio = text_to_speech(reply)
    else:
        st.session_state.last_audio = None


# ============================================================
# REMINDER CHECKER
# ============================================================

def check_reminders():
    """
    Check for due reminders.

    This function is called whenever the Streamlit app
    reruns.
    """

    reminders = due_reminders()

    for reminder in reminders:
        st.toast(
            f"⏰ Reminder: {reminder}",
            icon="⏰",
        )


check_reminders()


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("⚙️ Controls")

    st.session_state.speak = st.toggle(
        "🔊 Speak responses",
        value=st.session_state.speak,
    )

    st.divider()

    # --------------------------------------------------------
    # Tasks
    # --------------------------------------------------------

    st.subheader("📝 To-Do List")

    tasks = list_tasks()

    if tasks:

        for task_id, task_text in tasks:

            col1, col2 = st.columns([4, 1])

            col1.write(task_text)

            if col2.button(
                "✔",
                key=f"complete_{task_id}",
            ):
                complete_task(task_id)
                st.rerun()

    else:
        st.caption("No pending tasks.")

    st.divider()

    # --------------------------------------------------------
    # Reminders
    # --------------------------------------------------------

    st.subheader("⏰ Reminders")

    reminders = list_reminders()

    if reminders:

        for _, reminder_text, due in reminders:

            try:
                due_date = datetime.fromisoformat(due)

                formatted = due_date.strftime(
                    "%d %b %Y, %I:%M %p"
                )

            except ValueError:
                formatted = due

            st.write(
                f"• {reminder_text} — {formatted}"
            )

    else:
        st.caption("No pending reminders.")

    st.divider()

    # --------------------------------------------------------
    # User
    # --------------------------------------------------------

    saved_name = get_pref("name")

    if saved_name:
        st.info(
            f"👤 User: **{saved_name}**"
        )

    # --------------------------------------------------------
    # Clear Chat
    # --------------------------------------------------------

    if st.button(
        "🗑 Clear Chat",
        use_container_width=True,
    ):

        st.session_state.history = []

        st.session_state.last_audio = None

        st.rerun()


# ============================================================
# MAIN PAGE
# ============================================================

st.title("🤖 AI-Based Personal Assistant")

st.caption(
    "Your intelligent assistant for chat, reminders, "
    "tasks, voice commands, web searches and automation."
)


# ============================================================
# EXAMPLES
# ============================================================

with st.expander("💡 Example Commands"):

    st.markdown(
        """
        - `My name is Rahul`
        - `What time is it?`
        - `What is today's date?`
        - `Remind me to submit my project at 6 PM`
        - `Remind me to study in 30 minutes`
        - `Add task buy project notes`
        - `Show my tasks`
        - `Show my reminders`
        - `Open YouTube`
        - `Open calculator`
        - `Search Python tutorials`
        - `Explain artificial intelligence`
        """
    )


# ============================================================
# CHAT HISTORY
# ============================================================

for role, message in st.session_state.history:

    with st.chat_message(role):
        st.markdown(message)


# ============================================================
# AUDIO RESPONSE
# ============================================================

if st.session_state.last_audio:

    st.audio(
        st.session_state.last_audio,
        format="audio/mp3",
    )


# ============================================================
# VOICE INPUT
# ============================================================

st.subheader("🎙 Voice Command")

audio = st.audio_input(
    "Record your command"
)

if audio is not None:

    audio_id = getattr(
        audio,
        "file_id",
        None,
    )

    # Process only new recordings
    if audio_id != st.session_state.seen_audio:

        st.session_state.seen_audio = audio_id

        spoken_text = speech_to_text(
            audio.getvalue()
        )

        if (
            spoken_text
            and not spoken_text.startswith("[Could not")
        ):

            st.info(
                f"🎤 You said: **{spoken_text}**"
            )

            handle(spoken_text)

            st.rerun()

        else:

            st.warning(spoken_text)


# ============================================================
# TEXT INPUT
# ============================================================

prompt = st.chat_input(
    "Type your command..."
)

if prompt:

    handle(prompt)

    st.rerun()
