"""add job protection and soft delete

`protected` lets an owner lock a job so Clear All skips it and the v1 API
refuses to delete it. `deleted_at` turns Clear All into a recoverable delete:
the row and its files stay for a grace period, hidden from the owner's list
and from the job URLs, until `flask purge-deleted-jobs` removes them.

Revision ID: f3a9d2c7b1e4
Revises: c4e81a7b25d9
Create Date: 2026-10-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f3a9d2c7b1e4'
down_revision = 'c4e81a7b25d9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('job') as batch_op:
        batch_op.add_column(sa.Column(
            'protected', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_job_deleted_at', ['deleted_at'])


def downgrade():
    with op.batch_alter_table('job') as batch_op:
        batch_op.drop_index('ix_job_deleted_at')
        batch_op.drop_column('deleted_at')
        batch_op.drop_column('protected')
