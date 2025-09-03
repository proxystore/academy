# Globus Client End to End Test

This script validates that the cloud hosted exchange can properly use the Globus API to authenticate users, and that the GlobusExchangeClient can launch agents and delegate tokens to them.

**Running**

Create a Globus Client ID and Secret for the exchange, and fill in the missing fields in  `exchange_config.toml`. Launch the exchange:
```bash
python -m academy.exchange.cloud --config exchange_config.toml &
```

Replace the Project ID in the script with a Globus project that you own, then run the test script:
```bash
python run_globus_client_test.py
```
