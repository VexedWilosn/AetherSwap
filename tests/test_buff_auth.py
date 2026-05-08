import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import buff_auth


def test_buff_profile_segment_is_account_scoped_and_path_safe():
    assert buff_auth.safe_buff_profile_segment("acc01") == "acc01"
    assert buff_auth.safe_buff_profile_segment("acc/02:buff") == "acc_02_buff"
    assert buff_auth.safe_buff_profile_segment("") == "default"
