swarm relay serve \
  --host 0.0.0.0 \
  --port 9443 \
  --certfile ~/.swarm/tls/relay/cert.pem \
  --keyfile ~/.swarm/tls/relay/privkey.pem \
  --api-key-file ~/.config/swarm/relay.key
