"""Rabbi Consult e-Invoicing converter — core package.

The pipeline is: per-client *reader* -> common internal table (LineRow list)
-> shared *engine* -> Digitax CSV *output*. Everything after the reader is
shared by every client; adding a client means adding one reader.
"""
