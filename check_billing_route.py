import os
os.environ['DATABASE_URL'] = 'sqlite:///stock_armobile.db'
import app as stock_app
from app import db, User

stock_app.app.config['TESTING'] = True
stock_app.app.config['WTF_CSRF_ENABLED'] = False

with stock_app.app.app_context():
    db.drop_all()
    db.create_all()
    stock_app.ensure_database_schema()
    u = User(username='admin', email='admin@test.local', role='admin')
    u.set_password('admin123')
    db.session.add(u)
    db.session.commit()

    client = stock_app.app.test_client()
    r = client.post('/auth/login', data={'username': 'admin', 'password': 'admin123'})
    print('login', r.status_code, r.headers.get('Location'))
    r2 = client.get('/admin/billing')
    print('billing', r2.status_code)
    print(r2.data.decode('utf-8')[:400])
