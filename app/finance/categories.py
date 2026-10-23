"""Categories are free text in Telegram, so 'Food', 'food' and 'Clotes' all
arrive. Each spelling is folded to one canonical key: lowercase, then through
the alias table, which grows whenever categories are merged in the portal."""
import json
from pathlib import Path

GROUPS = {
    "living": "Living",
    "business": "Business",
    "project": "Project",
    "savings": "Savings",
}

# Every install starts with these; household.json adds its own on top.
DEFAULT_CATEGORIES = [
    ("food", "Food", "living", "groceries and eating out"),
    ("gas", "Gas", "living", "fuel"),
    ("car", "Car", "living", "maintenance, repairs, registration"),
    ("drinks", "Drinks", "living", "coffees and drinks out"),
    ("clothing", "Clothing", "living", ""),
    ("tech", "Tech", "living", "gadgets and electronics"),
    ("sw", "SW", "living", "software subscriptions"),
    ("tel", "Tel", "living", "phone bills"),
    ("insurance", "Insurance", "living", "insurance premiums"),
    ("vacation", "Vacation", "living", ""),
    ("other", "Other", "living", "anything else"),
]
DEFAULT_ALIASES = {
    "clothes": "clothing",
    "drink": "drinks",
    "fuel": "gas",
    "others": "other",
    "othr": "other",
}

# Household-specific categories, spellings and one-off clean-up merges live in
# household.json (git-ignored; household.example.json shows the format):
#   categories: [{key, label, group, notes}]  created if missing, group set
#   aliases:    {spelling: key}                folded on every message
#   merges:     {old key: new key}             applied to existing data by migration 4
HOUSEHOLD_FILE = Path(__file__).resolve().parent.parent / "household.json"
EXAMPLE_FILE = HOUSEHOLD_FILE.with_name("household.example.json")


def load_household(path=None):
    """The household settings, or empty ones when there is no file."""
    for candidate in ([Path(path)] if path else [HOUSEHOLD_FILE, EXAMPLE_FILE]):
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return {
                "categories": [(c["key"], c.get("label") or default_label(c["key"]), c.get("group", "living"),
                                c.get("notes", "")) for c in data.get("categories", [])],
                "aliases": data.get("aliases", {}),
                "merges": data.get("merges", {}),
            }
    return {"categories": [], "aliases": {}, "merges": {}}


def _apply_categories(conn, rows):
    for key, label, grp, notes in rows:
        conn.execute("INSERT OR IGNORE INTO categories (key, label, grp, description) VALUES (?, ?, ?, ?)",
                     (key, label, grp, notes))
        conn.execute("UPDATE categories SET grp = ? WHERE key = ?", (grp, key))
        conn.execute("UPDATE categories SET description = ? WHERE key = ? AND description = ''", (notes, key))


def seed(conn, household):
    _apply_categories(conn, DEFAULT_CATEGORIES + household["categories"])
    for alias, key in {**DEFAULT_ALIASES, **household["aliases"]}.items():
        conn.execute("INSERT OR IGNORE INTO categories (key, label) VALUES (?, ?)", (key, default_label(key)))
        conn.execute("INSERT OR IGNORE INTO category_aliases (alias, category_key) VALUES (?, ?)", (alias, key))


def cleanup(conn, household):
    """Household merges for data that is already in (every entry keeps its
    original spelling; "Split off" on a category's page undoes a merge)."""
    _apply_categories(conn, household["categories"])
    for source, target in household["merges"].items():
        conn.execute("INSERT OR IGNORE INTO categories (key, label) VALUES (?, ?)", (target, default_label(target)))
        merge(conn, source, target)


def default_label(spelled):
    # Keep deliberate capitals ("ViTech"); otherwise capitalise the first letter.
    if any(ch.isupper() for ch in spelled[1:]):
        return spelled
    return spelled[:1].upper() + spelled[1:].lower()


def resolve(db, raw):
    """Canonical category key for a spelling, creating the category if new."""
    spelled = raw.strip()
    alias = spelled.lower()
    row = db.execute("SELECT category_key FROM category_aliases WHERE alias = ?", (alias,)).fetchone()
    key = row["category_key"] if row else alias
    db.execute(
        "INSERT OR IGNORE INTO categories (key, label) VALUES (?, ?)",
        (key, default_label(spelled)),
    )
    return key


def edit_distance(a, b):
    """Single-letter edits (add, drop, change, swap two neighbours) to turn a into b."""
    before, previous = None, list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], before[j - 2] + 1)
        before, previous = previous, current
    return previous[-1]


TYPO_MIN_USES = 3  # only categories in regular use are offered as corrections


def suggest(db, raw):
    """The category `raw` is probably a typo of, or None.

    Only new spellings are checked; an existing category or alias is taken as
    meant. Up to 1 letter off for short words, 2 for words of 5+ letters; ties
    go to the more used category."""
    typed = raw.strip().lower()
    known = db.execute(
        "SELECT 1 FROM categories WHERE key = ? UNION SELECT 1 FROM category_aliases WHERE alias = ?",
        (typed, typed),
    ).fetchone()
    if known:
        return None
    limit = 1 if len(typed) <= 4 else 2
    best = None
    for key, uses in db.execute(
        "SELECT category, COUNT(*) FROM transactions GROUP BY category HAVING COUNT(*) >= ?", (TYPO_MIN_USES,)
    ):
        if abs(len(key) - len(typed)) > limit:
            continue
        distance = edit_distance(typed, key)
        if distance <= limit and (best is None or (distance, -uses) < (best[0], -best[1])):
            best = (distance, uses, key)
    return best[2] if best else None


def merge(db, source, target):
    """Fold category `source` into `target`, now and for future messages."""
    if source == target:
        return
    db.execute("UPDATE transactions SET category = ? WHERE category = ?", (target, source))
    db.execute("UPDATE category_aliases SET category_key = ? WHERE category_key = ?", (target, source))
    db.execute(
        "INSERT OR REPLACE INTO category_aliases (alias, category_key) VALUES (?, ?)",
        (source, target),
    )
    db.execute("DELETE FROM categories WHERE key = ?", (source,))


def split(db, alias):
    """Undo a merge for one spelling: entries typed that way get their own
    category again, and new messages with it stop being folded."""
    row = db.execute("SELECT category_key FROM category_aliases WHERE alias = ?", (alias,)).fetchone()
    if row is None:
        return False
    db.execute("DELETE FROM category_aliases WHERE alias = ?", (alias,))
    db.execute("INSERT OR IGNORE INTO categories (key, label) VALUES (?, ?)", (alias, default_label(alias)))
    db.execute(
        "UPDATE transactions SET category = ? WHERE category = ? AND lower(trim(category_raw)) = ?",
        (alias, row["category_key"], alias),
    )
    return True
