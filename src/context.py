"""Standing context for Max Smart: instructions, goals, and read-only memory.

This module only reads Markdown files. It never writes docs/memory.md.
"""

from pathlib import Path


def build_standing_context(project_root):
    """Read the three source files and return the assembled instructions string."""
    project_root = Path(project_root)

    # docs/max-smart-instructions.md (role, challenge, V1 limits)
    max_instructions = (project_root / "docs" / "max-smart-instructions.md").read_text(
        encoding="utf-8"
    )

    # docs/goals.md (priorities, preferences, boundaries)
    adam_goals = (project_root / "docs" / "goals.md").read_text(encoding="utf-8")

    # docs/memory.md — file-based, transparent, edited only by Adam
    long_term_memory = (project_root / "docs" / "memory.md").read_text(
        encoding="utf-8"
    )

    return (
        "=== Max Smart's standing instructions ===\n\n"
        + max_instructions
        + "\n\n---\n\n"
        + "=== Adam's goals ===\n\n"
        + adam_goals
        + "\n\n---\n\n"
        + "=== Max Smart's long-term memory ===\n\n"
        + "This memory is file-based, transparent, and edited only by Adam. "
        + "You may use it as context. You must not modify docs/memory.md "
        + "or treat yourself as able to write memory.\n\n"
        + long_term_memory
    )
