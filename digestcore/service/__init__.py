"""Core HTTP service: the single owner of the data, exposed as a JSON API.

Interfaces (OWUI, CLI, Slack, …) become thin clients of this service and never
touch the database directly. Standard-library only — no web framework.
"""
