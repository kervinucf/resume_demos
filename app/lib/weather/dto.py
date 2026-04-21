from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory

hc = create_hyper_server(
    root="geo",
    data_path=create_default_storage_directory(),
)

def read_pages():

    results = hc.children("geo.locations", page=1, per_page=200).items()
    page_count = 1
    while len(results) > 0:
        for location in results:
            print(location)
        page_count += 1
        results = hc.children("geo.locations", page=page_count, per_page=200).items()

read_pages()
"""


for location in results['_embedded'].items():
    print(location)
    

"""