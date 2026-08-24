"""reset active warnings when a ban is revoked

Revision ID: 0005_reset_warnings_on_unban
Revises: 0004_antispam_text
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_reset_warnings_on_unban"
down_revision: str | None = "0004_antispam_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TRIGGER_FUNCTION = r"""
CREATE OR REPLACE FUNCTION reset_group_warnings_after_unban()
RETURNS trigger AS $$
BEGIN
    IF OLD.action = 'ban'
       AND OLD.is_active = TRUE
       AND NEW.is_active = FALSE THEN
        UPDATE moderation_actions
        SET is_active = FALSE,
            revoked_at = COALESCE(revoked_at, NOW())
        WHERE chat_id = NEW.chat_id
          AND target_user_id = NEW.target_user_id
          AND action = 'warning'
          AND is_active = TRUE;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER = r"""
CREATE TRIGGER trg_reset_group_warnings_after_unban
AFTER UPDATE OF is_active ON moderation_actions
FOR EACH ROW
EXECUTE FUNCTION reset_group_warnings_after_unban();
"""


def upgrade() -> None:
    op.execute(TRIGGER_FUNCTION)
    op.execute("DROP TRIGGER IF EXISTS trg_reset_group_warnings_after_unban ON moderation_actions")
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_reset_group_warnings_after_unban ON moderation_actions")
    op.execute("DROP FUNCTION IF EXISTS reset_group_warnings_after_unban()")
