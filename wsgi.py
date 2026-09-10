from flask import flash, redirect, request, url_for, jsonify
from flask_login import current_user, login_required
from sqlalchemy import text