# Vercel front door

PaperCI's agent cannot run on Vercel: it needs Docker sandboxes, multi-hour jobs and a long-lived worker. This
project only gives it a clean `*.vercel.app` URL. Every request is rewritten at Vercel's edge to the EC2 agent
(`https://44-215-70-124.sslip.io`), which still serves the app, board, API and reviews.

    cd deploy/vercel
    vercel link --yes --project paperci
    vercel deploy --prod --yes
