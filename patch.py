with open('data/streamer.py', 'r', encoding='utf-8') as f:
    text = f.read()

injection = '''
    async def update_subscriptions(self, symbols: list[str], incremental: bool = False) -> None:
        \"\"\"
        Hot-swap WebSocket subscriptions without dropping the TLS connection.
        If incremental is True, uses "ADD" command. Otherwise, uses "SUBS" to overwrite.
        \"\"\"
        self._symbols = list(symbols)
        if getattr(self, '_active_ws', None) is None or self._active_ws.closed:
            logger.warning("SchwabStreamer: No active websocket to update subscriptions. Will be picked up on next reconnect.")
            return

        cmd = "ADD" if incremental else "SUBS"
        subs_request = {
            "requests": [
                {
                    "service":    "LEVELONE_EQUITIES",
                    "requestid":  str(_next_request_id()),
                    "command":    cmd,
                    "SchwabClientCustomerId": self._client_ids.get("SchwabClientCustomerId", ""),
                    "SchwabClientCorrelId":   self._client_ids.get("SchwabClientCorrelId", ""),
                    "parameters": {
                        "keys":   ",".join(self._symbols),
                        "fields": self._fields,
                    },
                }
            ]
        }
        await self._active_ws.send(json.dumps(subs_request))
        logger.info(
            "SchwabStreamer: hot-swapped LEVELONE_EQUITIES using %s command for %s.",
            cmd, self._symbols
        )

    # ------------------------------------------------------------------'''

text = text.replace('    # ------------------------------------------------------------------\n    # Thread lifecycle / Event Loop', injection + '\n    # Thread lifecycle / Event Loop')

with open('data/streamer.py', 'w', encoding='utf-8') as f:
    f.write(text)
