#!/usr/bin/env python3
"""One-off probe: list the models on a system so we can spot the RFI document."""
import sys
import warnings

from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

from istari_digital_client import Configuration
from istari_digital_client.sdk import Istari

client = Istari(Configuration())
system_id = sys.argv[1]
s = client.systems.get(system_id)
print("system:", s.name, "|", (s.description or "")[:80])
for b in client.systems.branches.list(system_id):
    for tr in client.systems.branches.list_files(b):
        print(f"branch={b.tag} type={tr.resource_type} name={tr.name!r} "
              f"id={tr.resource_id} mime={tr.mime}")
