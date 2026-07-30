import os
import sys
import django
from django.test import Client

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'BestLogMarketPlaceProject.settings')
proj_root = os.path.dirname(os.path.dirname(__file__))
if proj_root not in sys.path:
    sys.path.insert(0, proj_root)

django.setup()
from BestLogMarketPlaceApp.models import Product

c = Client()
prod = Product.objects.first()
if prod:
    prod.description = "2024 account.\nYou can login using the Facebook app or website.\nUSA account with an already-created page."
    prod.save()
    r = c.get(f'/product/{prod.id}/')
    print('GET product detail status', r.status_code)
    content = r.content.decode('utf-8')
    idx = content.find('2024 account')
    print('description present:', idx!=-1)
    print('linebreaks present (br tag):', '<br' in content[idx:idx+200])
    # also print surrounding snippet
    if idx!=-1:
        print(content[idx:idx+300])
else:
    print('no product found')
