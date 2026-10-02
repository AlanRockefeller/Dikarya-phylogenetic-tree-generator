"""add paused voucher sync run status

A scan the user interrupts is resumable: its rows and parameters are already
stored, so continuing it only has to scan the observations it had not reached.
That is a different outcome from `cancelled`, which means "stopped, start
again", so it gets its own status rather than being inferred from a row count.

Only the CHECK constraint changes; no data is touched, and existing rows keep
whatever status they already have.

Revision ID: c4e81a7b25d9
Revises: b7d4e2f1a9c3
Create Date: 2026-10-01 03:10:00.000000

"""
from alembic import op

revision = 'c4e81a7b25d9'
down_revision = 'b7d4e2f1a9c3'
branch_labels = None
depends_on = None

CONSTRAINT = 'ck_voucher_sync_run_status'
OLD = "status in ('queued', 'running', 'completed', 'cancelled', 'failed')"
NEW = "status in ('queued', 'running', 'completed', 'cancelled', 'paused', 'failed')"


def upgrade():
    # batch_alter_table so SQLite (which cannot drop a constraint in place)
    # rebuilds the table instead of failing.
    with op.batch_alter_table('voucher_sync_run', schema=None) as batch_op:
        batch_op.drop_constraint(CONSTRAINT, type_='check')
        batch_op.create_check_constraint(CONSTRAINT, NEW)


def downgrade():
    # A paused run would violate the old constraint, so fold those back into
    # cancelled first -- they stay readable, they just stop being resumable.
    op.execute("UPDATE voucher_sync_run SET status = 'cancelled' WHERE status = 'paused'")
    with op.batch_alter_table('voucher_sync_run', schema=None) as batch_op:
        batch_op.drop_constraint(CONSTRAINT, type_='check')
        batch_op.create_check_constraint(CONSTRAINT, OLD)
