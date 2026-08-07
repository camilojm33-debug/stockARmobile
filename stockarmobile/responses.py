"""Reusable response helpers."""

from flask import flash, jsonify, redirect, url_for


def api_error(message, http_status=400, **extra):
    payload = {"success": False, "error": str(message)}
    payload.update(extra)
    return jsonify(payload), http_status


def api_success(status=200, **extra):
    payload = {"success": True}
    payload.update(extra)
    return jsonify(payload), status


def flash_redirect(message, category, endpoint, **url_kwargs):
    flash(message, category)
    return redirect(url_for(endpoint, **url_kwargs))
