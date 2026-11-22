"""lfm's three Textual themes, and the one stylesheet they dress."""

from dataclasses import replace

from textual.theme import BUILTIN_THEMES, Theme

# Two themes, same app: warm paper and ink blue, with the orange pointing at
# whatever has the keyboard. Text keeps its contrast in both (measured: body
# 12.1:1 dark, 10.0:1 light; accent 7.9:1 and 4.7:1 against the background).
LFM_OBSIDIAN = Theme(
    name="lfm-obsidian",
    primary="#7aa2d6",
    secondary="#7aa2d6",
    accent="#7aa2d6",
    foreground="#e8e3da",
    background="#232332",
    surface="#2a2a3d",
    panel="#313148",
    boost="#3a3a52",
    success="#6fbf73",
    warning="#ffd166",
    error="#ff6b6b",
    dark=True,
    # the row highlight: $accent at 25% over the background, opaque
    variables={
        # The accent mixed into the background, as the rule above describes -- but at 12%,
        # not the orange's 25%: a blue bright enough to read as text (5.87:1 here) is
        # bright enough that a quarter of it lifts the row past where the muted play count
        # still clears 4.5:1. Measured: at 12% the row holds the same contrast against the
        # page the orange row had (1.22:1), and the muted ink on it measures 4.64:1.
        "block-cursor-background": "#2d3246",
        "block-cursor-foreground": "#e8e3da",
        "logo": "#7aa2d6",
        # The equalizer strip's one colour: the ramp is shaded from it, so the strip
        # belongs to this theme rather than running green to red through a blue one.
        "spectrum": "#7aa2d6",
    },
)
LFM_CREAM = Theme(
    name="lfm-cream",
    primary="#7a3800",
    secondary="#40607f",
    accent="#7a3800",
    # Rich's `dim` — what the row builders use for play counts — is 60% of this,
    # and at #2b2620 that lands on the paper at only 4.14:1. Darker ink lifts it.
    foreground="#14100c",
    background="#dad2be",
    surface="#e2dbc9",
    panel="#cdc3ab",
    boost="#c3b89f",
    # Darkened a shade each, because these three carry text as well as marks and had not
    # been measured against the paper: success was 3.29:1 on the page and its green marker
    # 2.59:1 on the row, warning 3.69:1, error 4.49:1. As they stand now: success 4.81:1 on
    # the page and 3.79:1 on the row, warning 4.80:1 and 3.79:1, error 4.68:1 and 3.69:1.
    success="#316139",
    warning="#735100",
    error="#983939",
    dark=False,
    # `text-muted` is a solid colour rather than Textual's `auto 60%`: the derived one is
    # only 3.5:1 on this row, and no `block-cursor-background` can fix that (the row must stay
    # darker than the paper to be a row at all). #4a4235 clears 4.5:1 on page, panel and row.
    variables={
        "block-cursor-background": "#cdb99a",
        "block-cursor-foreground": "#14100c",
        "text-muted": "#4a4235",
        "logo": "#7a3800",
    },
)
# midnight is Textual's own dark palette -- what lfm looked like before it had themes of
# its own -- registered under a name of ours. Two things follow from that. The name has to
# be ours, because a registered theme that reuses a built-in's name does not carry our
# variables: the stylesheet then compiles `$logo` against the built-in and fails. And two
# palette entries have to be replaced rather than inherited, because Textual's own are
# wrong for this app: its row highlight is a saturated blue, which measured the muted play
# count at 2.57:1 and the playing marker at 1.94:1 against it, and its warning is the same
# amber as the accent, so a warning carried no signal of its own.
#
# The highlight is set the way obsidian's is set -- the accent mixed into the background --
# and the foreground with it, so the row stays opaque and legible. The wordmark is plain
# white here, as it always was: the accent is what the rest of the app points with, and a
# logo wearing it reads as a link.
_TEXTUAL_DARK = BUILTIN_THEMES.get("textual-dark")
LFM_MIDNIGHT = (
    replace(
        _TEXTUAL_DARK,
        name="lfm-midnight",
        variables={
            **_TEXTUAL_DARK.variables,
            "logo": "#ffffff",
            # $accent #fea62b at 25% over the background #121212, composited opaque
            "block-cursor-background": "#4d3718",
            "block-cursor-foreground": "#e0e0e0",
            "warning": "#ffd166",
            "error": "#ff6b6b",
        },
    )
    if _TEXTUAL_DARK is not None
    else None  # a Textual without its dark palette: midnight falls back to obsidian
)

# The rotation `T` walks, in this order.
THEMES: dict[str, str] = {
    "obsidian": LFM_OBSIDIAN.name,
    "cream": LFM_CREAM.name,
    "midnight": LFM_MIDNIGHT.name if LFM_MIDNIGHT is not None else LFM_OBSIDIAN.name,
}
DEFAULT_THEME = "obsidian"

# One motion scale: three rhythms, all eased out so a change starts when the key does
# and settles rather than stopping. A state that answers the keyboard takes STATE; a
# button under the pointer takes PRESS; a surface that changes with the whole theme
# takes SURFACE. The stylesheet below names them as __PRESS__, __STATE__ and
# __SURFACE__, replaced once at import -- braces in CSS make an f-string awkward.
PRESS_MS, STATE_MS, SURFACE_MS = 80, 120, 200
PRESS, STATE, SURFACE = (f"{ms}ms out_cubic" for ms in (PRESS_MS, STATE_MS, SURFACE_MS))

# Shared look, so every screen feels like one app.
CSS = """
$scrollbar: $accent 35%;
$scrollbar-hover: $accent 60%;
$scrollbar-active: $accent;
/* a faint trough so the track is visible, low enough to stay out of the way */
$scrollbar-background: $foreground 8%;
$scrollbar-background-hover: $foreground 14%;
$scrollbar-background-active: $foreground 20%;
/* A slim band instead of Textual's Header: one row, the theme's panel, and
   the screen's name where the eye already is. */
#band { height: 1; background: $panel; padding: 0 2; }
/* $logo, not $accent: every theme defines it, and the black one defines it white */
#band-name { width: auto; text-style: bold; color: $logo; }
#band-context { width: 1fr; text-align: right; color: $text-muted; }
/* Spacing: page gutters are 2, gutters inside a column are 1, sections are a
   row apart. One scale, so screens line up without per-screen values. */
Screen { layout: vertical; }
/* Textual's default vertical scrollbar is two cells wide, and the style is not
   inherited from the screen: every widget needs it. */
* { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }
.section {
    text-style: bold;
    color: $accent;
    padding-top: 1;
}
#tracks-tabs {
    padding-top: 1; /* the gap .section asked for */
}
/* The active tab is a block, not only an ink: the tint crosses from one tab to the
   next, so the shift stays visible while the keyboard is in the tags window and the
   accent is off. 14% of the accent is the most that keeps the accent label at 4.5:1;
   8% of the foreground is enough of a block for the muted label to stay legible on. */
#tracks-tabs Tab {
    text-style: bold;
    color: $text-muted;
    background: transparent;
    transition: color __STATE__, background __STATE__;
}
#tracks-tabs Tab.-active { background: $foreground 8%; }
/* the accent marks the window the keyboard is in, not the tab that was chosen */
#tracks-tabs.in-focus Tab.-active { color: $accent; background: $accent 14%; }
#album-meta {
    color: $text-muted;
    padding-left: 1;
    display: none;
}
#tags-label { text-style: bold; color: $text-muted; transition: color __STATE__; }
#tags-label.in-focus { color: $accent; }
#empty-hint {
    color: $text-muted;
    padding: 1 1;
    display: none;
}
#title {
    text-style: bold;
    color: $text;
    background: transparent;
    transition: background __STATE__;
    /* hug the artist name with a cell either side, rather than a full-width band */
    width: auto;
    padding: 0 1;
    margin-bottom: 1;
}
/* the accent band fades in on mount, so the page settles rather than arrives lit */
#title.lit { background: $accent 30%; }
/* the band and its numbers share a row, with air between them */
#title-row { height: auto; }
#meta { color: $text-muted; padding-left: 2; }
/* Ten tracks before the list scrolls (the rows are one line each), whatever the
   bio's length; the page gives, and scrolls to reach what is under the window. */
#tags { height: 10; }
#tracks { height: 10; }
/* a readable measure */
#bio {
    color: $text-muted;
    height: auto;
    max-width: 90;
}
#spectrum { height: 1; padding: 0 2; }
ListView { background: transparent; }
ListView > ListItem { padding: 0 1; }
ListView > ListItem > Horizontal { width: 100%; height: auto; }
RowLabel { width: 1fr; }
.status { color: $success; width: 2; }
/* A paused row is not a failure, so it takes the muted ink rather than the warning
   colour. The shape carries the state; this carries "and it is not going". */
.status-paused { color: $text-muted; }
/* The row's tint must be opaque. A translucent one lets Textual composite each
   child's own cells against the screen instead of the row, which broke the band
   wherever the text was. Every theme defines the block-cursor colours, so those
   carry the tint and our themes set them to the accent's shade. */
/* The highlighted row is Textual's own `-highlight`, untinted from here: our
   tint - and tinting the content inside the row with it - left a stripe behind
   every row the cursor passed, because a class on the row is not re-resolved for
   the widgets inside it. The theme's block-cursor colours are the same shade. */
/* The highlight means "this is what the keyboard is on", so a list that does
   not have focus shows no highlighted row at all. */
ListView:blur > ListItem.-highlight { background: transparent; }
/* the bottom row is air: without it the bio's last line sits on the player bar */
#content { height: 1fr; padding: 1 2 1 2; }
/* The columns take what is left of the page, so they can never fall below its
   fold; the lists scroll inside them (like the similar list) rather than
   growing the page until the selection walks off the bottom. */
#columns { height: auto; }
#tracks-column, #tags-column { height: auto; }
#tracks-column { width: 3fr; padding-right: 2; }
#tags-column { width: 1fr; }
/* `h`: the key sheet over a dimmed screen, wearing the same clothes as the rest:
   a tinted title band, the panel background of the player bar, no border. */
ShortcutsScreen { background: $background 60%; align: center middle; }
/* A popup, centred on a dimmed page: ModalScreen dims what is underneath, so the sheet
   reads as something on top of lfm rather than as another page in it. */
ShortcutsScreen { align: center middle; }
#shortcuts-panel {
    /* a definite width, or the sheet inside cannot work out how tall it is */
    width: 90%;
    max-width: 84;
    height: auto;      /* the box fits its keys */
    max-height: 90%;   /* and scrolls when the terminal is short */
    padding: 0 2;
    background: $panel;
    border: round $primary;
}
/* 16: wide enough for `Tab / Shift+Tab`, the longest merged pair the sheet writes */
/* width: 100%: the panel's background is painted per row where its content reaches,
   so a narrower container left the page showing along the top and bottom rows. */
#shortcut-columns { height: auto; width: 100%; }
.shortcut-column { width: 1fr; height: auto; padding-right: 2; }
/* five rows: a row of padding either side, the title, a row of air under it, controls */
#player-bar {
    dock: bottom;
    height: 5;
    padding: 1 2;
    background: $panel;
    transition: background __SURFACE__;
}
#now-playing-row { height: 1; margin-bottom: 1; }
#now-playing { width: 1fr; text-style: bold; text-overflow: ellipsis; overflow: hidden hidden; }
/* the bar's right half: where you are, repeat, volume */
#bar-info { width: auto; padding-left: 2; color: $text-muted; }
#controls { height: 1; }
#playing { width: auto; padding: 0 1; }
/* the bar is ours, drawn as one line of text: a gradient fill, a dim rest */
#progress { width: 1fr; height: 1; }
/* "Start" and "Pause" are both five cells, and nine leaves four to split two and
   two, so both labels sit exactly centred. Labels of different lengths cannot
   share a fixed width: the odd cell always falls on one side, which is what made
   "Pause" look off-centre next to a four-cell "Play". */
#toggle { width: 9; }
#why { height: 1; color: $text-muted; padding: 0 1; }
.player-button {
    border: none;
    height: 1;
    min-width: 8;
    margin-right: 1;
    background: $accent 20%;
    color: $text;
    transition: background __PRESS__;
}
.player-button:hover { background: $accent 40%; }
.player-button:focus { background: $accent 55%; }
#wordmark { padding: 1 2 1 2; height: auto; }  /* the art, and a row of air under it */
#home-title { padding: 0 2; text-style: bold; }
/* One width for the landing column, so the heading, the prompt and the list
   share a middle line. The input has no side margin: its margins would shrink
   the column the others centre in and put it two cells to the right. */
#artist {
    margin: 1 0;
    border: tall $accent 50%;
    width: 60;
}
#artist:focus { border: tall $accent; }  /* instant: a keyboard cue must not lag */
/* Every remembered artist reaches the list, but ten rows are shown by default:
   the rest scroll, so the page ends where the eye expects it to. */
#recent { height: auto; max-height: 10; width: 100%; max-width: 60; }
#landing { align-horizontal: center; height: 1fr; }
/* `max-width`, not a fixed 60: below that the column and its heading parted company,
   the list ran off the edge, and the page grew a horizontal scrollbar. */
#wordmark, #home-title, #recent-title { width: 100%; max-width: 60; text-align: center; }
/* The instructions belong at the foot of the window, not under the prompt. */
#hint { dock: bottom; padding: 0 2; text-align: center; color: $text-muted; }
#recent-title { padding: 1 2 0 2; }  /* top: the gap .section asks for */
#recent ListItem { padding: 0 1; }
"""

CSS = CSS.replace("__PRESS__", PRESS).replace("__STATE__", STATE).replace("__SURFACE__", SURFACE)
