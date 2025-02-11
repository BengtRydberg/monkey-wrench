from monkey_wrench.query import EumetsatAPI


def test_eumetsat_api_seviri_collection_url():
    assert (
            "https://api.eumetsat.int/data/download/1.0.0/collections/EO%3AEUM%3ADAT%3AMSG%3AHRSEVIRI/products" ==
            EumetsatAPI.seviri_collection_url().unicode_string()
    )
