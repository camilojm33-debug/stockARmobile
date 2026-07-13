import app as stock_app
from app import db, User, Company, Product

stock_app.app.config['TESTING'] = True
stock_app.app.config['WTF_CSRF_ENABLED'] = False
with stock_app.app.app_context():
    db.drop_all(); db.create_all(); stock_app.ensure_database_schema()
    c = Company(name='Empresa t1', active=True)
    db.session.add(c); db.session.flush()
    u = User(username='tenant1', email='tenant1@test.local', company_id=c.id, role='user', active=True)
    u.set_password('abc12345')
    db.session.add(u); db.session.commit()

client = stock_app.app.test_client()
r = client.post('/auth/login', data={'username':'tenant1','password':'abc12345'}, follow_redirects=False)
print('login', r.status_code, r.headers.get('Location'))
r = client.get('/productos/')
print('products_get', r.status_code)
r = client.post('/productos/add', data={
    'barcode':'', 'name':'Prod X','description':'desc','category':'cat','sale_type':'unidad','unit_measure':'u',
    'brand':'b','supplier':'s','cost_price':'10','price':'20','margin':'10','profit_percent':'100','stock':'5','min_stock':'1','discount':'0'
}, follow_redirects=False)
print('add_post', r.status_code, r.headers.get('Location'))
if r.status_code in (301,302):
    rr = client.get(r.headers.get('Location') or '/', follow_redirects=False)
    print('redir_status', rr.status_code)
with stock_app.app.app_context():
    p = Product.query.filter_by(name='Prod X').first()
    print('product', bool(p), p.company_id if p else None)
