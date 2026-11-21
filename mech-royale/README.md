# Mech Royale (Builders Buildathon)

This folder holds our bot for the Builders Mech Royale track.

- `mech-royale/bot/` – the bot (TypeScript, runs on Bun). `bot.ts` is the brain, `map.ts` rasterizes the campus map for line of sight and path finding, `mechfight.ts` is the kit's protocol wrapper.
- `mech-royale/kit/` – the official starter kit (protocol, schema, conformance check, map).
- `mech-royale/sim/` – our local match simulator for testing.
- `Dockerfile.mech` builds the bot image. The scorer needs a Dockerfile at the top of the repo, and `main`'s belongs to another track, so submissions use the `mech-royale-submit` branch, whose top-level `Dockerfile` is a copy of `Dockerfile.mech`.

Check the image the way the scorer does:

```sh
docker build -f mech-royale/Dockerfile.mech -t ttun-bot .
cd mech-royale/kit && python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" ttun-bot
```

Code we did not write: the starter kit files from Builders (`mech-royale/kit/`, `mech-royale/bot/mechfight.ts`). Released under the MIT License.
