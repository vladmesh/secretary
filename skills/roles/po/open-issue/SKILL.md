---
name: open-issue
description: "File a product issue on the board as the PO: check the product and the open issues for a duplicate, agree kind, priority, title and description with the owner, and create it with `secretary issue create`. Use on requests like 'file an issue', 'add this to the backlog', 'open an issue', `$open-issue`."
---

# Open Issue

An issue is a durable record on the product's board: a bug, a feature, a question or an improvement
the product owes. It is input for future sprints, not a plan. Your work ends at the created issue; you
do not cut cards, promote it to a sprint or start work on it.

## 1. Find the product and check for a duplicate

```bash
python3 -P -m secretary product list
python3 -P -m secretary issue list --product <product>
python3 -P -m secretary issue show --ref <issue-ref>
```

If an open issue already covers the problem, do not file a second one. Add what is new to it instead:

```bash
python3 -P -m secretary issue append --role po --ref <issue-ref> \
  --reason "<why this block is added>" --body-file <file>
```

## 2. Agree the fields with the owner

- `--kind`: `bug`, `feature`, `question` or `improvement`.
- `--priority`: `P0` to `P3`. The owner decides priority; propose one and say why.
- `--title`: one line that names the problem, not the fix.
- `--description`: what is observed or wanted, the evidence (commands, outputs, refs) and what is
  deliberately out of it. Enough for an observer who was not in this conversation.

Show the owner the fields before creating the issue.

## 3. Create it

```bash
python3 -P -m secretary issue create --role po --product <product> \
  --kind <kind> --priority <P0-P3> --title "<title>" --description "<description>" \
  --request-id <stable-id>
```

`--request-id` makes a retry safe: repeating the command with the same id does not create a second
issue. Report the issue reference the command prints back to the owner.

## Later changes

- Priority: `issue update-priority --role po --ref <ref> --priority <P0-P3> --reason "<why>"`.
- Close: `issue close --role po --ref <ref> --reason resolved|invalid|duplicate|wont_do`.
