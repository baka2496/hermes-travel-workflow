"""Read Hermes's conversation databases (profiles/*/state.db): the one place that knows their layout.

The reply-check plugin and page_evidence.py both look up what tools ran and what they returned.
Each used to open the databases and parse the rows itself. Hermes owns that schema and may change
it in an upgrade, and both readers turned any database error into "nothing found", so a renamed
column would have switched every check off without a word. Now the layout is named here once,
`schema_problems()` says when a database no longer matches it, and `setup.py check` reports that.

Installed beside the scripts (shared/travel-workflow) and inside the plugin folder.
"""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3

# The columns of Hermes's `messages` table the workflow reads (Hermes 0.23.0).
MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp")


def profile_databases(root):
    """Each profile's conversation database under a Hermes root (the workflow's bots)."""
    return sorted((Path(root) / "profiles").glob("*/state.db"))


def databases(home):
    """Every conversation database under a Hermes home: its own state.db, then each profile's.
    `home` may be the Hermes root or one profile's folder (a plugin's hook gets either)."""
    home = Path(home)
    return ([home / "state.db"] if (home / "state.db").is_file() else []) + profile_databases(home)


@contextmanager
def read_only(path, timeout=5):
    """A read-only connection; never writes, never creates the file."""
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=timeout)) as db:
        yield db


def schema_problem(db):
    """Why this database does not look like the layout above, or None when it does."""
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    except sqlite3.Error as error:
        return f"cannot read: {error}"
    if not columns:
        return "no messages table"
    missing = [c for c in MESSAGE_COLUMNS if c not in columns]
    return f"messages table lacks {', '.join(missing)}" if missing else None


def schema_problems(home):
    """[(database, problem)] for each database under `home` that the checks could not read."""
    problems = []
    for path in databases(home):
        try:
            with read_only(path) as db:
                problem = schema_problem(db)
        except sqlite3.Error as error:
            problem = f"cannot open: {error}"
        if problem:
            problems.append((path, problem))
    return problems


def calls(tool_calls_json):
    """The calls in a `tool_calls` value as (call id, tool name, arguments text).
    Hermes stores both {"id", "function": {"name", "arguments"}} and flat {"call_id", "name", "arguments"}."""
    try:
        parsed = json.loads(tool_calls_json or "[]")
    except ValueError:
        return []
    found = []
    for call in parsed if isinstance(parsed, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function", call)
        if not isinstance(function, dict):
            continue
        found.append((call.get("id") or call.get("call_id"), str(function.get("name", "")),
                      str(function.get("arguments", ""))))
    return found


def result_text(content):
    """A tool result's text: the "output" of a JSON result, else the content itself."""
    try:
        output = json.loads(content).get("output", "")
    except (ValueError, TypeError, AttributeError):
        output = content or ""
    return str(output or "").replace("\r\n", "\n").strip()


class Recent(dict):
    """A dict that keeps only the `limit` most recently set keys, for per-conversation notes the
    plugin keeps in memory (one gateway runs for weeks; the notes only matter for the current turn)."""

    def __init__(self, limit=200):
        super().__init__()
        self.limit = limit

    def __setitem__(self, key, value):
        self.pop(key, None)
        super().__setitem__(key, value)
        while len(self) > self.limit:
            del self[next(iter(self))]

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


NOT_THE_USER = ("[CONTEXT COMPACTION", "[IMPORTANT:")
def owner_words(home, session_id=None):
    """What the user typed in this conversation (`session_id`), or, without one, in the latest
    Telegram conversation (Hermes's `sessions.source`): the one the bot is answering. None when no
    database can be read, so a check that needs them can stand aside instead of calling everything unsaid.

    Only this conversation counts. 2026-11-25: other Telegram conversations held test requests such as
    「12/14 想去礁溪泡湯，2人，預算 2000」, so a week of messages let the made-up NT$2,000 through; a budget
    that outlives one conversation belongs in preferences.json with the owner's `quote`.
    """
    paths, latest = [], None
    for path in databases(home):
        try:
            with read_only(path) as db:
                db.execute("SELECT 1 FROM messages LIMIT 1")
                if session_id:
                    paths.append(path)
                    continue
                # Without a conversation, only a database that says which ones are Telegram's counts
                # (Antigravity's review of 0783149: an older layout returned [] and flagged every budget).
                row = db.execute("SELECT m.timestamp, m.session_id FROM messages m JOIN sessions s ON "
                                 "s.id = m.session_id WHERE m.role = 'user' AND s.source = 'telegram' "
                                 "ORDER BY m.timestamp DESC LIMIT 1").fetchone()
                paths.append(path)
                if row and (latest is None or row[0] > latest[0]):
                    latest = row
        except sqlite3.Error:  # unreadable, or an older layout without `sessions`
            continue
    if not paths:
        return None
    wanted = session_id or (latest[1] if latest else None)
    words = []
    for path in paths if wanted else []:
        try:
            with read_only(path) as db:
                words += [row[0] for row in db.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (wanted,))]
        except sqlite3.Error:
            continue
    # Hermes stores its own notes as user messages too: a compacted summary repeats what the agent
    # wrote (an invented budget included), and a preloaded skill is not the user talking.
    return [str(w) for w in words if w and not str(w).startswith(NOT_THE_USER)]
