#!/usr/bin/env python3
"""EvoClaw entry point — run with: python run.py"""
import asyncio
from host.main import main

if __name__ == "__main__":
    asyncio.run(main())
