from sqlalchemy import update
from sqlmodel import Session, SQLModel, create_engine, select

from app.database import PlatformAction
from app.services.trading.actions import (
    InvalidPlatformActionTransition,
    claim_due_actions,
    create_platform_action,
    transition_action,
)
from app.services.trading.states import PlatformActionState, PlatformActionType


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform_action.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return Session(engine)


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform_action.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return engine


def test_create_platform_action_is_idempotent(tmp_path):
    with _session(tmp_path) as session:
        action1, created1 = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="BUFF",
            item_id=123,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=2,
        )
        action2, created2 = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=123,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=2,
        )

        assert created1 is True
        assert created2 is False
        assert action1.id == action2.id
        assert action1.platform == "buff"
        assert action1.risk_category == "ak-47 | redline"
        assert action1.locked_budget_cny == 200


def test_transition_action_rejects_invalid_terminal_jump(tmp_path):
    with _session(tmp_path) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="item",
            target_price=10,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=9)
        transition_action(action, PlatformActionState.SUCCEEDED, now=10)
        session.add(action)
        session.commit()

        try:
            transition_action(action, PlatformActionState.PROCESSING)
        except InvalidPlatformActionTransition:
            pass
        else:
            raise AssertionError("terminal action should not move back to processing")


def test_claim_due_actions_sets_processing_lease_and_recovers_expired_lease(tmp_path):
    with _session(tmp_path) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="item",
            target_price=10,
            next_check_at=90,
        )

        claimed = claim_due_actions(session, now=100, limit=5, lease_seconds=30)
        assert [x.id for x in claimed] == [action.id]
        assert claimed[0].state == PlatformActionState.PROCESSING
        assert claimed[0].lease_until == 130

        assert claim_due_actions(session, now=120, limit=5, lease_seconds=30) == []

        reclaimed = claim_due_actions(session, now=131, limit=5, lease_seconds=30)
        assert [x.id for x in reclaimed] == [action.id]
        assert reclaimed[0].lease_until == 161

        rows = session.execute(select(PlatformAction)).scalars().all()
        assert len(rows) == 1


def test_claim_due_actions_does_not_return_rows_taken_by_another_worker(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="item",
            target_price=10,
            next_check_at=90,
        )
        action_id = action.id

    stolen = False
    real_execute = Session.execute

    def stealing_execute(self, statement, *args, **kwargs):
        nonlocal stolen
        text = str(statement)
        if not stolen and "UPDATE platform_action" in text:
            stolen = True
            with Session(engine) as other:
                other.execute(
                    update(PlatformAction)
                    .where(PlatformAction.id == action_id)
                    .where(PlatformAction.state == PlatformActionState.QUEUED)
                    .values(
                        state=PlatformActionState.PROCESSING,
                        updated_at=99,
                        lease_until=129,
                    )
                )
                other.commit()
        return real_execute(self, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", stealing_execute)
        with Session(engine) as session:
            claimed = claim_due_actions(session, now=100, limit=5, lease_seconds=30)

    assert claimed == []
    with Session(engine) as session:
        row = session.get(PlatformAction, action_id)
        assert row.state == PlatformActionState.PROCESSING
        assert row.lease_until == 129
