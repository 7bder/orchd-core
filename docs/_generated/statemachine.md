# 状态表（生成物，勿手改）

## AMEND

- allowed_from: （不受约束）
- target: （条件分支/无变更）

## CLAIMED

- allowed_from: pending
- target: claimed

## DONE

- allowed_from: claimed
- target: done

## FORCE_STATUS

- allowed_from: （不受约束）
- target: （条件分支/无变更）

## RETRACT

- allowed_from: （不受约束）
- target: （条件分支/无变更）

## REVIEW_CLAIMED

- allowed_from: （不受约束）
- target: （条件分支/无变更）

## REVIEW_READY

- allowed_from: done, in_review
- target: in_review

## REVIEW_SUBMITTED

- allowed_from: in_review
- target: （条件分支/无变更）
