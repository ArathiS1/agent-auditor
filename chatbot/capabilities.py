"""What this chat app can do, by the ids used in the Auditor's feature comparison (data/feature_matrix.json).

The list is sent to the Auditor inside the registered version, so the dashboard's Gap analysis always reflects the app that is
actually running. Only list a feature here when it works; tests check that each one has code behind it.
"""
FEATURES = [
    "streaming", "markdown", "copy", "stop", "regenerate", "edit_message", "feedback", "followups", "search_chats", "export",
    "voice_in", "files", "images", "web_search", "deep_research", "memory", "model_switch", "dark_mode", "shows_reasoning",
]
EXTRAS = ["compare_models", "inspect_trace"]  # things the Auditor adds that other assistants do not have
