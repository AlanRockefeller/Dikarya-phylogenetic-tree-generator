"""add voucher sync tables

Revision ID: b7d4e2f1a9c3
Revises: a1b2c3d4e5f6
Create Date: 2026-08-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d4e2f1a9c3'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('inat_user_credential',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('access_token_enc', sa.Text(), nullable=False),
    sa.Column('jwt_enc', sa.Text(), nullable=True),
    sa.Column('jwt_created_at', sa.Integer(), nullable=True),
    sa.Column('inat_login', sa.String(length=64), nullable=True),
    sa.Column('inat_user_id', sa.Integer(), nullable=True),
    sa.Column('scope', sa.String(length=64), nullable=False, server_default=''),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_inat_user_credential_user_id'),
    )

    op.create_table('voucher_sync_run',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False, server_default='scan'),
    sa.Column('status', sa.String(length=16), nullable=False, server_default='queued'),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('progress_done', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('progress_total', sa.Integer(), nullable=True),
    sa.Column('rows', sa.JSON(), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('parent_run_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("kind in ('scan', 'apply')", name='ck_voucher_sync_run_kind'),
    sa.CheckConstraint(
        "status in ('queued', 'running', 'completed', 'cancelled', 'failed')",
        name='ck_voucher_sync_run_status'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_run_id'], ['voucher_sync_run.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('voucher_sync_run', schema=None) as batch_op:
        batch_op.create_index('ix_voucher_sync_run_user_created', ['user_id', 'created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('voucher_sync_run', schema=None) as batch_op:
        batch_op.drop_index('ix_voucher_sync_run_user_created')
    op.drop_table('voucher_sync_run')
    op.drop_table('inat_user_credential')
