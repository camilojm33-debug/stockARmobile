import os
import sys
from pathlib import Path

root = Path(r'c:\Users\USUARIO\Desktop\stock ultimate')
os.chdir(root)

import app as stock_app
from app import User, db

stock_app.app.config["WTF_CSRF_ENABLED"] = False

with stock_app.app.app_context():
    db.drop_all()
    db.create_all()
    stock_app.ensure_database_schema()
    user = User(username='admin', email='admin@test.local', role='admin')
    user.set_password('admin123')
    db.session.add(user)
    db.session.commit()

client = stock_app.app.test_client()
root_response = client.get('/')
print('ROOT_STATUS', root_response.status_code)
print('ROOT_HAS_LANDING', 'StockArmobile' in root_response.get_data(as_text=True))
protected_response = client.get('/dashboard/')
print('DASHBOARD_STATUS', protected_response.status_code)
login_response = client.post('/auth/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=False)
print('LOGIN_STATUS', login_response.status_code)
print('LOGIN_LOCATION', login_response.headers.get('Location'))
