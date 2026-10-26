from app.schemas.examples import ExampleItem

EXAMPLE_ITEMS: tuple[ExampleItem, ...] = tuple(
    ExampleItem(id=f"ex-{index}", display_name=f"Ejemplo {index}")
    for index in range(1, 26)
)


def list_example_items() -> list[ExampleItem]:
    """Lista fija de ejemplos para demostrar paginación. Sin persistencia."""

    return list(EXAMPLE_ITEMS)


def get_example_item(example_id: str) -> ExampleItem | None:
    for item in EXAMPLE_ITEMS:
        if item.id == example_id:
            return item
    return None
