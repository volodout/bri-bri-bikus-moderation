from moderation.b2b_client import B2BClient
from moderation.config import Settings
from moderation.database import ModerationStore
from moderation.http_app import serve
from moderation.product_events import ProductEventService


def main() -> None:
    settings = Settings.from_env()
    store = ModerationStore(settings.database_path)
    store.ensure_schema()
    b2b_client = B2BClient(settings.b2b_base_url, settings.mod_to_b2b_key)
    product_event_service = ProductEventService(store, b2b_client)
    serve(settings.host, settings.port, product_event_service, settings.b2b_to_mod_key)


if __name__ == "__main__":
    main()
