#!/usr/bin/env python3
import asyncio
import json
import os
import unittest
from watch_me import State, state_socket_server, WORK_SECONDS


class TestSocketServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = State()
        # Set a predictable state
        self.state.work_accumulated = 150.0  # 150 seconds elapsed
        self.state.on_break = False

        # Run the socket server task
        self.server_task = asyncio.create_task(state_socket_server(self.state))
        # Give the server a moment to start
        await asyncio.sleep(0.1)

        # Get the socket path
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime_dir or not os.path.isdir(runtime_dir):
            runtime_dir = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
            try:
                os.makedirs(runtime_dir, exist_ok=True)
            except OSError:
                import tempfile
                runtime_dir = tempfile.gettempdir()
        self.socket_path = os.path.join(runtime_dir, "watch-me.sock")

    async def asyncTearDown(self):
        # Cancel server task and wait for it to complete
        self.server_task.cancel()
        try:
            await self.server_task
        except asyncio.CancelledError:
            pass

        # Verify that the socket file is cleaned up
        self.assertFalse(os.path.exists(self.socket_path))

    async def test_socket_payload(self):
        # Verify the socket file was created
        self.assertTrue(os.path.exists(self.socket_path))

        # Connect to the UNIX domain socket
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            # Read line from socket
            line = await reader.readline()
            data = json.loads(line.decode("utf-8"))

            # Assert expected keys and values
            self.assertEqual(data["status"], "ACTIVE")
            self.assertEqual(data["work_elapsed"], 150)
            self.assertEqual(data["work_remaining"], WORK_SECONDS - 150)
            self.assertEqual(data["idle_elapsed"], 0)
            self.assertFalse(data["on_break"])
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
