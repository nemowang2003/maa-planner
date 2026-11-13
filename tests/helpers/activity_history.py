from tests.helpers.scheduling import timestamp as ts


def history(name="墟", *, start="2025-08-02T12:00:00", end="2025-08-23T03:59:59"):
    return {
        "activityInfoMap": {
            "act44side": {
                "id": "act44side",
                "name": name,
                "type": "SIDESTORY",
                "isReplicate": False,
                "startTime": ts(start),
                "endTime": ts(end),
                "rewardEndTime": ts(end) + 7 * 86400,
            }
        },
        "activity": [{"actId": "act44side", "actReplicaId": "act44sre", "zones": []}],
    }
