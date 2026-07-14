# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from bottle import request, response

from quasarr.constants import FILECRYPT_ENABLED_TABLE
from quasarr.storage.sqlite_database import DataBase


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _read_filecrypt_enabled():
    filecrypt_db = DataBase(FILECRYPT_ENABLED_TABLE)
    return _coerce_bool(filecrypt_db.retrieve("filecrypt"), default=True)


def refresh_filecrypt_setting(shared_state):
    value = _read_filecrypt_enabled()
    shared_state.update("filecrypt_enabled", value)
    return value


def initialize_filecrypt_setting(shared_state):
    return refresh_filecrypt_setting(shared_state)


def get_filecrypt_setting_data(shared_state):
    response.content_type = "application/json"
    return {"success": True, "enabled": refresh_filecrypt_setting(shared_state)}


def save_filecrypt_setting(shared_state):
    response.content_type = "application/json"

    data = request.json
    if not isinstance(data, dict):
        return {"success": False, "message": "Invalid JSON payload"}

    filecrypt_db = DataBase(FILECRYPT_ENABLED_TABLE)
    current_value = _read_filecrypt_enabled()
    next_value = _coerce_bool(data.get("enabled"), current_value)
    filecrypt_db.update_store("filecrypt", "true" if next_value else "false")

    value = refresh_filecrypt_setting(shared_state)

    return {
        "success": True,
        "message": "Filecrypt setting saved successfully",
        "enabled": value,
    }
