"""Category taxonomy for personal memory.

Memories are tagged with one or more categories (life domains such as finance,
property or employment). Categories are recorded on the episode's
``source_description`` in a parseable form, so every fact extracted from an
episode inherits that episode's categories via ``EntityEdge.episodes``. All
memories live in a single group so facts in different categories stay
connected in one graph.

This module is free of I/O so it can be unit-tested without a database or LLM.
"""

import re

from config.schema import CategoryConfig

GENERAL_CATEGORY = 'general'

_TAG_PATTERN = re.compile(r'(?:^|;\s*)categories=([^;]*)')
_AGENT_PATTERN = re.compile(r'(?:^|;\s*)agent=([^;]*)')


def normalize_category(name: str) -> str:
    """Lower-case a category name and collapse separators to underscores."""
    return re.sub(r'[^a-z0-9]+', '_', name.strip().lower()).strip('_')


def encode_source_description(
    categories: list[str], agent: str | None = None, extra: dict[str, str] | None = None
) -> str:
    """Build a source_description carrying the agent, category and any extra tags."""
    parts = []
    if agent:
        parts.append(f'agent={agent.replace(";", ",").strip()}')
    parts.append(f'categories={",".join(categories)}')
    for key, value in (extra or {}).items():
        parts.append(f'{key}={value.replace(";", ",").strip()}')
    return '; '.join(parts)


def parse_categories(source_description: str | None) -> list[str]:
    """Extract the category tags written by encode_source_description."""
    if not source_description:
        return []
    match = _TAG_PATTERN.search(source_description)
    if not match:
        return []
    return [c for c in (normalize_category(c) for c in match.group(1).split(',')) if c]


def parse_agent(source_description: str | None) -> str | None:
    """Extract the agent written by encode_source_description."""
    if not source_description:
        return None
    match = _AGENT_PATTERN.search(source_description)
    return match.group(1).strip() if match else None


class CategoryTaxonomy:
    """The configured categories, with keyword inference and related-category expansion."""

    def __init__(self, categories: list[CategoryConfig]):
        self.categories: dict[str, CategoryConfig] = {}
        for category in categories:
            self.categories[normalize_category(category.name)] = category
        self._keyword_patterns: dict[str, list[re.Pattern[str]]] = {
            name: [
                re.compile(rf'\b{re.escape(keyword.lower())}(?:s|es)?\b', re.IGNORECASE)
                for keyword in [name.replace('_', ' '), *category.keywords]
                if keyword.strip()
            ]
            for name, category in self.categories.items()
        }

    def normalize(self, names: list[str] | None) -> list[str]:
        """Normalize and de-duplicate category names, preserving order.

        Unknown names are kept so agents can introduce new categories ad hoc.
        """
        result: list[str] = []
        for name in names or []:
            normalized = normalize_category(name)
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    def infer(self, text: str) -> list[str]:
        """Return the configured categories whose name or keywords appear in text.

        Keywords match whole words, allowing a plural "s"/"es" suffix.
        """
        return [
            name
            for name, patterns in self._keyword_patterns.items()
            if any(pattern.search(text) for pattern in patterns)
        ]

    def resolve(self, categories: list[str] | None, text: str) -> list[str]:
        """Categories given explicitly, otherwise inferred, otherwise general."""
        resolved = self.normalize(categories) or self.infer(text)
        return resolved or [GENERAL_CATEGORY]

    def expand(self, names: list[str]) -> list[str]:
        """Add the related categories of each name (one hop), preserving order."""
        expanded = list(names)
        for name in names:
            category = self.categories.get(name)
            if category is None:
                continue
            for related in self.normalize(category.related):
                if related not in expanded:
                    expanded.append(related)
        return expanded

    def search_hint(self, name: str) -> str:
        """Text used to search for memories belonging to a category."""
        category = self.categories.get(name)
        if category is None:
            return name.replace('_', ' ')
        keywords = ', '.join(category.keywords[:12])
        hint = f'{name.replace("_", " ")}: {category.description}'
        return f'{hint} ({keywords})' if keywords else hint

    def describe(self) -> list[dict[str, object]]:
        """Serializable view of the taxonomy for the list_categories tool."""
        return [
            {
                'name': name,
                'description': category.description,
                'keywords': category.keywords,
                'related': self.normalize(category.related),
            }
            for name, category in self.categories.items()
        ]
