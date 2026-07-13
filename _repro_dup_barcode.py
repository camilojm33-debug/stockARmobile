import app as stock_app
from app import db, User, Company, Product

stock_app.app.config['TESTING']=True
stock_app.app.config['WTF_CSRF_ENABLED']=False
with stock_app.app.app_context():
    db.drop_all(); db.create_all(); stock_app.ensure_database_schema()
    c1=Company(name='A', active=True); c2=Company(name='B', active=True)
    db.session.add_all([c1,c2]); db.session.flush()
    u=User(username='u2', email='u2@test.local', role='user', active=True, company_id=c2.id); u.set_password('x12345')
    p=Product(barcode='SAME1', name='Prod A', company_id=c1.id, active=True, price=1)
    db.session.add_all([u,p]); db.session.commit()

client=stock_app.app.test_client(); client.post('/auth/login', data={'username':'u2','password':'x12345'})
r=client.post('/productos/add', data={'barcode':'SAME1','name':'Prod B','price':'10','sale_type':'unidad','unit_measure':'u'}, follow_redirects=False)
print('status', r.status_code, 'loc', r.headers.get('Location'))
print('body_prefix', r.get_data(as_text=True)[:120])
