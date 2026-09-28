"""
Credit Underwriting Agent.

Reads the recorded results of the agents before it (KYC, income consistency,
eligibility, risk, document verification, bank-statement signals) plus a
bureau report, interprets them under a configured underwriting policy, and
produces an evidence-linked UNDERWRITING ASSESSMENT for the Decision Agent.

It never approves or rejects, and never re-computes what another agent owns.
"""
