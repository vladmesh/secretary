# Kanboard v1.2.46 comment contract evidence

Secretary pins `kanboard/kanboard:v1.2.46`. Normalized comment recovery relies on two producer
properties of that version, verified from upstream source and an isolated disposable backend on
2026-09-05. No live or shared board was used.

The pinned upstream `CommentModel::getAll` query orders first by `comments.date_creation` and then
by the auto-increment `comments.id`, using the same requested direction for both. The API calls it
with its default `ASC`, so equal-second comments have a stable creation-id tie-break rather than an
unspecified database order. `CommentModel::create` returns the persisted comment id or `false`, and
`CommentProcedure::createComment` returns that value after validation. The public API contract also
describes success as `comment_id` and failure as `false`.

Source:

- [Pinned CommentModel at v1.2.46](https://github.com/kanboard/kanboard/blob/839585162a77ac8e9cd9b7c74c7e5ed9da785e72/app/Model/CommentModel.php#L57-L93)
- [Pinned CommentProcedure at v1.2.46](https://github.com/kanboard/kanboard/blob/839585162a77ac8e9cd9b7c74c7e5ed9da785e72/app/Api/Procedure/CommentProcedure.php#L53-L87)
- [Official comment API contract](https://docs.kanboard.org/v1/api/comment_procedures/)

The canary ran the pinned Docker image with a temporary SQLite volume and disposable API token. It
created one temporary project and task, then submitted 50 `createComment` members in one JSON-RPC
batch. The response contained 50 positive integer ids, 1 through 50, in 0.24 seconds. A subsequent
`getAllComments` returned all 50 bodies in creation order. Every row had the same `date_creation`
second, so the canary exercised the source's `id ASC` tie-break directly. The container used
`--rm`; it was stopped and its temporary data was removed after the read.

Recovery therefore treats `(date_creation, id) ASC` as the supported producer order for the pinned
backend. A future Kanboard version change must repeat this source check and disposable canary before
changing the pin. Restore writes use at most 50 comment creates per HTTP request, well below the
existing 30-second transport timeout in this canary. A timeout is still ambiguous and retains
per-occurrence pending evidence for reconciliation; the timing is evidence for the chosen bound,
not an absolute service SLO.
