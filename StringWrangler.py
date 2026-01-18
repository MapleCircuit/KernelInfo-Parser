# based on tools from http://github.com/mbeware
import textwrap

try:
    from wcwidth import wcwidth
    __UNICODE_SUPPORT = True
except ImportError:
    print("***** Warning : Install package wcwidth to use unicode utilities *****")
    __UNICODE_SUPPORT = False


__STRING_WRANGLER_DEFAULT_TAG = "###"

def align_columns(rows, padding=2):
    widths = [
        max(len(row[i]) for row in rows)
        for i in range(len(rows[0]))
    ]

    aligned = []
    for row in rows:
        aligned.append([
            cell.ljust(widths[i] + padding)
            for i, cell in enumerate(row)
        ])

    return aligned

def tag_lines(lines, prefix=__STRING_WRANGLER_DEFAULT_TAG):
    return [f"{prefix}{line}" for line in lines]

def group_lines(lines, groupingFunction):
    """
    group lines each time the function is true for a line.
    Function example : 
    - str.isdigit() : A new number for numbered list
    - str.startswith(prefix): If processing a markdown file, this could be # 
    - str.endswith(suffix): If processing a filelist, this could be a certain extendion  
    """
    groups = []
    current = []

    for line in lines:
        if groupingFunction(line) and current:
            groups.append(current)
            current = []
        current.append(line)

    if current:
        groups.append(current)

    return groups
def normalize(text):
    """
    Remove extra white spaces from string
    """
    return " ".join(text.strip().split())

def listify(items):
    """
    Create list from string
    """
    return [str(item) for item in items]



def __wrap_lines_no_split(lines, max_len):
    """
    Do not split words
    """
    
    
    groups = []


    for line in lines:
        wrapped = textwrap.wrap(line, max_len)
        if not wrapped:
            wrapped = [line]

        groups.append(wrapped)

    return groups

def wrap_lines(lines, max_len, no_split=True):
    """
    Wrap lines and return groups of strings
    """
    assert isinstance(lines, list), "lines must be a list of string"
    assert lines , "Need at least 1 line"
    assert max_len > 0, "can't split string to less than 1 characters"
    
    
    groups = []

    if no_split: 
        groups = __wrap_lines_no_split(lines, max_len)

    else: 
        for line in lines:
            if not line:
                groups.append([""])
                continue

            wrapped = []
            remaining = line

            while remaining:
                wrapped.append(remaining[:max_len])
                remaining = remaining[max_len:]

            groups.append(wrapped)

    return groups



def render_with_indent(groups, indent=__STRING_WRANGLER_DEFAULT_TAG):
    """
    Add indentation to group of strings
    """
    rendered = []

    for group in groups:
        if not group:
            rendered.append([])
            continue

        out = [group[0]]
        for line in group[1:]:
            out.append(indent + line)

        rendered.append(out)

    return rendered

def render_ansi_box(groups):
    """
    create a box aroud a group of string
    """
    rendered = []

    for group in groups:
        if not group:
            rendered.append([])
            continue

        width = max(len(line) for line in group)

        top =    f"┌{'─' * (width + 2)}┐"
        bottom = f"└{'─' * (width + 2)}┘"

        boxed = [top]
        for line in group:
            boxed.append(f"│ {line.ljust(width)} │")
        boxed.append(bottom)

        rendered.append(boxed)

    return rendered


if __UNICODE_SUPPORT: 
    def visible_len_unicode(text):
        """
        Retourne la largeur visible d'une chaîne en colonnes terminal.
        """
        width = 0
        for ch in text:
            w = wcwidth(ch)
            if w > 0:
                width += w
        return width
else:
    def visible_len_unicode(text):
        return NotImplemented


if __UNICODE_SUPPORT: 
    def pad_to_visible_width_unicode(text, target_width):
        """
        Pad une chaîne avec des espaces pour atteindre une largeur visible cible.
        """
        padding = target_width - visible_len_unicode(text)
        if padding > 0:
            return text + (" " * padding)
        return text
else:
    def pad_to_visible_width_unicode(text, target_width):
        return NotImplemented

if __UNICODE_SUPPORT:
    def render_ansi_box_unicode(groups):
        """
        create a box around unicode variable width characters
        """
        rendered = []

        for group in groups:
            if not group:
                rendered.append([])
                continue

            content_width = max(visible_len_unicode(line) for line in group)

            top =    f"┌{'─' * (content_width + 2)}┐"
            bottom = f"└{'─' * (content_width + 2)}┘"

            boxed = [top]
            for line in group:
                padded = pad_to_visible_width_unicode(line, content_width)
                boxed.append(f"│ {padded} │")
            boxed.append(bottom)

            rendered.append(boxed)

        return rendered
else:
    def render_ansi_box_unicode(groups):
        return NotImplemented   

