import re
import app as stock_app
from app import db, User, Company, Product

stock_app.app.config['TESTING'] = True
with stock_app.app.app_context():
    db.drop_all()
    db.create_all()
    stock_app.ensure_database_schema()
    stock_app.create_admin_user()
    stock_app.ensure_primary_superadmin()

client = stock_app.app.test_client()

def csrf_from(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None

r = client.get('/auth/register')
csrf = csrf_from(r.get_data(as_text=True))
print('register_get', r.status_code, 'csrf?', bool(csrf))
r = client.post('/auth/register', data={
    'csrf_token': csrf,
    'username': 'tenant1',
    'email': 'tenant1@test.local',
    'password': 'abc12345'
}, follow_redirects=False)
print('register_post', r.status_code, r.headers.get('Location'))

r = client.get('/auth/login')
csrf = csrf_from(r.get_data(as_text=True))
print('login_get', r.status_code, 'csrf?', bool(csrf))
r = client.post('/auth/login', data={
    'csrf_token': csrf,
    'username': 'tenant1',
    'password': 'abc12345'
}, follow_redirects=False)
print('login_post', r.status_code, r.headers.get('Location'))

with stock_app.app.app_context():
    u = User.query.filter_by(username='tenant1').first()
    print('user_company_id', u.company_id, 'active', u.active, 'role', u.role)
    c = Company.query.filter_by(id=u.company_id).first()
    print('company_exists', bool(c), 'company_active', c.active if c else None, 'trial_ends', c.trial_ends_at if c else None)

r = client.get('/productos/')
print('products_get', r.status_code)
html = r.get_data(as_text=True)
csrf = csrf_from(html)
print('products_csrf?', bool(csrf))

r = client.post('/productos/add', data={
    'csrf_token': csrf,
    'barcode': '',
    'name': 'Prod X',
    'description': 'desc',
    'category': 'cat',
    'sale_type': 'unidad',
    'unit_measure': 'u',
    'brand': 'b',
    'supplier': 's',
    'cost_price': '10',
    'price': '20',
    'margin': '10',
    'profit_percent': '100',
    'stock': '5',
    'min_stock': '1',
    'discount': '0'
}, follow_redirects=False)
print('add_post', r.status_code, r.headers.get('Location'))
if r.status_code in (301, 302):
    rr = client.get(r.headers.get('Location') or '/')
    print('redirect_target_status', rr.status_code, 'target', r.headers.get('Location'))

with stock_app.app.app_context():
    p = Product.query.filter_by(name='Prod X').first()
    print('product_created', bool(p), 'product_company_id', p.company_id if p else None)
