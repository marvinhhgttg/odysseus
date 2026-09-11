import logging
from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse, JSONResponse

# We import the setup functions here to avoid circular imports in app.py
from routes.auth_routes import setup_auth_routes
from routes.upload_routes import setup_upload_routes
from routes.emoji_routes import setup_emoji_routes
from routes.session_routes import setup_session_routes
from routes.admin_wipe_routes import setup_admin_wipe_routes
from routes.memory.memory_routes import setup_memory_routes
from routes.skills_routes import setup_skills_routes
from routes.chat_routes import setup_chat_routes
from routes.research.research_routes import setup_research_routes
from routes.history.history_routes import setup_history_routes
from routes.search_routes import setup_search_routes
from routes.preset_routes import setup_preset_routes
from routes.diagnostics_routes import setup_diagnostics_routes
from routes.cleanup_routes import setup_cleanup_routes
from routes.personal_routes import setup_personal_routes
from routes.embedding_routes import setup_embedding_routes
from routes.model_routes import setup_model_routes
from routes.copilot_routes import setup_copilot_routes
from routes.chatgpt_subscription_routes import setup_chatgpt_subscription_routes
from routes.tts_routes import setup_tts_routes
from routes.stt_routes import setup_stt_routes
from routes.document_routes import setup_document_routes
from routes.signature_routes import setup_signature_routes
from routes.gallery.gallery_routes import setup_gallery_routes
from routes.editor_draft_routes import setup_editor_draft_routes
from routes.task_routes import setup_task_routes
from routes.assistant_routes import setup_assistant_routes
from routes.calendar_routes import setup_calendar_routes
from routes.shell_routes import setup_shell_routes
from routes.cookbook_routes import setup_cookbook_routes
from routes.workspace_routes import setup_workspace_routes
from routes.hwfit_routes import setup_hwfit_routes
from routes.compare_routes import setup_compare_routes
from routes.prefs_routes import setup_prefs_routes
from routes.backup_routes import setup_backup_routes
from routes.font_routes import setup_font_routes
from routes.mcp_routes import setup_mcp_routes
from routes.webhook_routes import setup_webhook_routes
from routes.api_token_routes import setup_api_token_routes
from routes.google_drive_organizer_routes import setup_google_drive_organizer_routes
from routes.note_routes import setup_note_routes
from routes.email_routes import setup_email_routes
from routes.codex_routes import setup_codex_routes, setup_claude_routes
from routes.vault_routes import setup_vault_routes
from routes.contacts.contacts_routes import setup_contacts_routes
from companion import setup_companion_routes
from routes.google_oauth_routes import router as google_oauth_router
from routes.briefing_routes import setup_briefing_routes

def register_all_routes(app: FastAPI, components: dict):
    """Centralized registration of all application routers."""
    
    # Extract managers from components for easier access
    session_manager = components["session_manager"]
    chat_handler = components["chat_handler"]
    chat_processor = components["chat_processor"]
    memory_manager = components["memory_manager"]
    memory_vector = components.get("memory_vector")
    research_handler = components["research_handler"]
    upload_handler = components["upload_handler"]
    webhook_manager = components["webhook_manager"]
    skills_manager = components["skills_manager"]
    config = components["config"]
    preset_manager = components["preset_manager"]
    rag_manager = components["rag_manager"]
    rag_available = components["rag_available"]
    personal_docs_mgr = components["personal_docs_manager"]
    model_discovery = components["model_discovery"]
    tts_service = components["tts_service"]
    stt_service = components["stt_service"]
    task_scheduler = components["task_scheduler"]
    api_key_manager = components["api_key_manager"]
    mcp_manager = components["mcp_manager"]
    google_drive_organizer_service = components["google_drive_organizer_service"]
    auth_manager = components["auth_manager"]

    # Auth
    auth_router = setup_auth_routes(auth_manager)
    app.include_router(auth_router)

    # Uploads
    upload_router, upload_cleanup_func = setup_upload_routes(upload_handler)
    app.include_router(upload_router)

    # Emojis
    app.include_router(setup_emoji_routes())

    # Sessions
    session_config = {
        "REQUEST_TIMEOUT": components["constants"]["REQUEST_TIMEOUT"], 
        "OPENAI_API_KEY": components["constants"]["OPENAI_API_KEY"], 
        "SESSIONS_FILE": components["constants"]["SESSIONS_FILE"]
    }
    app.include_router(setup_session_routes(session_manager, session_config, webhook_manager=webhook_manager))

    # Admin Wipe
    app.include_router(setup_admin_wipe_routes(session_manager))

    # Memory & Skills
    memory_router = setup_memory_routes(memory_manager, session_manager, memory_vector=memory_vector)
    app.include_router(memory_router)
    app.include_router(setup_skills_routes(skills_manager))

    # Chat
    app.include_router(setup_chat_routes(
        session_manager, chat_handler, chat_processor,
        memory_manager, research_handler, upload_handler,
        memory_vector=memory_vector,
        webhook_manager=webhook_manager,
        skills_manager=skills_manager,
    ))

    # Research
    app.include_router(setup_research_routes(research_handler, session_manager=session_manager))

    # History & Search
    app.include_router(setup_history_routes(session_manager))
    app.include_router(setup_search_routes(config))

    # Presets & Diagnostics
    app.include_router(setup_preset_routes(preset_manager))
    app.include_router(setup_diagnostics_routes(rag_manager, rag_available, research_handler, memory_vector))

    # Cleanup & Personal Docs
    app.include_router(setup_cleanup_routes(session_manager))
    app.include_router(setup_personal_routes(personal_docs_mgr, rag_manager, rag_available))

    # Embedding & Models
    app.include_router(setup_embedding_routes())
    app.include_router(setup_model_routes(model_discovery))

    # Third-party Logins (Copilot / ChatGPT)
    app.include_router(setup_copilot_routes())
    app.include_router(setup_chatgpt_subscription_routes())

    # Speech Services
    app.include_router(setup_tts_routes(tts_service))
    app.include_router(setup_stt_routes(stt_service))

    # Documents & Assets
    document_router = setup_document_routes(session_manager, upload_handler)
    app.include_router(document_router)
    app.include_router(setup_signature_routes())
    app.include_router(setup_gallery_routes())
    app.include_router(setup_editor_draft_routes())

    # Tasks & Agents
    app.include_router(setup_task_routes(task_scheduler))
    app.include_router(setup_assistant_routes(task_scheduler))

    # Calendar, Shell, Cookbook, Workspace
    calendar_router = setup_calendar_routes()
    app.include_router(calendar_router)
    app.include_router(setup_shell_routes())
    app.include_router(setup_cookbook_routes())
    app.include_router(setup_workspace_routes())
    app.include_router(setup_hwfit_routes())

    # Comparison & Prefs
    app.include_router(setup_compare_routes(session_manager))
    app.include_router(setup_prefs_routes())

    # Backup & UI Support
    app.include_router(setup_backup_routes(memory_manager, preset_manager, skills_manager))
    app.include_router(setup_font_routes())

    # MCP
    app.include_router(setup_mcp_routes(mcp_manager))

    # Webhooks & API Tokens
    app.include_router(setup_webhook_routes(webhook_manager, auth_manager, session_manager, api_key_manager))
    app.include_router(setup_api_token_routes())

    # Google Drive Organizer
    app.include_router(setup_google_drive_organizer_routes(google_drive_organizer_service))

    # Notes & Email
    app.include_router(setup_note_routes(task_scheduler))
    email_router = setup_email_routes()
    app.include_router(email_router)

    # Codex / Claude bridge
    app.include_router(setup_codex_routes(
        email_router=email_router,
        memory_router=memory_router,
        calendar_router=calendar_router,
        document_router=document_router,
    ))
    app.include_router(setup_claude_routes())

    # Vault & Contacts
    app.include_router(setup_vault_routes())
    app.include_router(setup_contacts_routes())

    # Companion & Google OAuth
    app.include_router(setup_companion_routes())
    app.include_router(google_oauth_router)

    # PM Briefing aggregation (read-only dashboard tile)
    app.include_router(setup_briefing_routes())

    return {"upload_cleanup_func": upload_cleanup_func}
