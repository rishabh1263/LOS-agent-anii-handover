"""
Crime Record Verification -- a sub-agent of Credit Underwriting: the criminal record check of the applicant and the
co-applicant through a crime-check vendor (a MOCK provider in demo mode).

    api/         the HTTP routes, mounted on the LOS server under /api/v1/credit/crime-verification
    domain/      request models and the field rules
    services/    the workflow (initiate, refresh, finalize), read views, the PDF report, access and errors
    providers/   the vendor boundary and the mock CrimeCheck adapter
    db/          the agent's own PostgreSQL database and its migrations
    demo_data/   the synthetic applications loader
    frontend/    the React screen (built to frontend/dist, served at /crime-verification/)

Config: app/config/credit_underwriting/crime_verification{,_wording,_demo}.yaml.
"""
