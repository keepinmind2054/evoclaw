"""Tests for AI Auto-Patch static security validator (Project D)."""
from __future__ import annotations

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.self_update_ai_fix import _validate_patch_content

def test_validate_safe_diff():
    # A safe diff modifying host/main.py (fixing a syntax error)
    safe_diff = """diff --git a/host/main.py b/host/main.py
index 1234567..89abcde 100644
--- a/host/main.py
+++ b/host/main.py
@@ -10,6 +10,6 @@
-def hello():
-    print("hello")
+def hello():
+    print("hello world")
"""
    assert _validate_patch_content(safe_diff) is True


def test_validate_unsafe_network_import():
    # Attempting to import socket
    unsafe_diff = """diff --git a/host/main.py b/host/main.py
--- a/host/main.py
+++ b/host/main.py
@@ -5,2 +5,3 @@
+import socket
+s = socket.socket()
"""
    assert _validate_patch_content(unsafe_diff) is False


def test_validate_unsafe_network_call():
    # Attempting to use requests.get
    unsafe_diff = """diff --git a/host/main.py b/host/main.py
--- a/host/main.py
+++ b/host/main.py
@@ -5,2 +5,3 @@
+res = requests.get("http://evil.com")
"""
    assert _validate_patch_content(unsafe_diff) is False


def test_validate_unsafe_subprocess():
    # Attempting to import and use subprocess
    unsafe_diff = """diff --git a/host/main.py b/host/main.py
--- a/host/main.py
+++ b/host/main.py
@@ -5,2 +5,3 @@
+import subprocess
+subprocess.run(["rm", "-rf", "/"])
"""
    assert _validate_patch_content(unsafe_diff) is False


def test_validate_unsafe_shell_execution():
    # Attempting to use shell=True
    unsafe_diff = """diff --git a/host/main.py b/host/main.py
--- a/host/main.py
+++ b/host/main.py
@@ -5,2 +5,3 @@
+os.system("echo hello", shell=True)
"""
    assert _validate_patch_content(unsafe_diff) is False


def test_validate_unsafe_file_modification():
    # Attempting to modify _tools.py
    unsafe_diff = """diff --git a/container/agent-runner/_tools.py b/container/agent-runner/_tools.py
--- a/container/agent-runner/_tools.py
+++ b/container/agent-runner/_tools.py
@@ -5,2 +5,3 @@
+def new_tool():
+    pass
"""
    assert _validate_patch_content(unsafe_diff) is False


def test_validate_unsafe_security_bypass():
    # Attempting to modify authorization
    unsafe_diff = """diff --git a/host/main.py b/host/main.py
--- a/host/main.py
+++ b/host/main.py
@@ -5,2 +5,3 @@
+is_authorized = True  # bypass authorization check
"""
    assert _validate_patch_content(unsafe_diff) is False
