import os
os.environ['DATABASE_URL'] = 'sqlite:///stock_armobile.db'
import app as stock_app
from app import db

with stock_app.app.app_context():
    db.create_all()
    stock_app.ensure_database_schema()
    print('schema_ok')
