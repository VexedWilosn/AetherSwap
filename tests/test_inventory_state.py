import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.state import State


def test_inventory_cache_is_scoped_by_account():
    state = State()

    state.set_inventory([{"assetid": "a1", "name": "A"}], account_id="acc-a")
    state.set_inventory([{"assetid": "b1", "name": "B"}], account_id="acc-b")

    assert state.get_inventory("acc-a") == [{"assetid": "a1", "name": "A"}]
    assert state.get_inventory("acc-b") == [{"assetid": "b1", "name": "B"}]
    assert state.get_inventory_meta("acc-a")["account_id"] == "acc-a"
    assert state.get_inventory_meta("acc-b")["account_id"] == "acc-b"
