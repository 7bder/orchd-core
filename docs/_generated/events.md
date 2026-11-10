# 事件表（生成物，勿手改）

## AMEND

- required: （无）
- optional: fields, hint, rationale, reason

## CLAIMED

- required: （无）
- optional: files_claimed, role

## DONE

- required: （无）
- optional: attempt_count, changes_description, concerns, verify

## FORCE_STATUS

- required: （无）
- optional: assignee, evidence_sha, reason, target_status, test_data

## RETRACT

- required: target_event_id
- optional: disposition, reason

## REVIEW_CLAIMED

- required: （无）
- optional: baseline_sha, is_self_review, review_type

## REVIEW_READY

- required: （无）
- optional: review_type

## REVIEW_SUBMITTED

- required: verdict
- optional: comments, is_self_review, merge_pending, merge_warning, review_type, rework_scope
