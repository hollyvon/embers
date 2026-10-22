# Security

## Never paste transcripts

Embers works on private conversation records. Do not paste transcript text, summaries of real sessions, database files, API keys, or real home paths into an issue, pull request, discussion, or commit. If you need to show a failure, reduce it to a synthetic example or describe the shape of the data.

If you pasted something sensitive by mistake, delete the post or edit it out and tell the maintainer so the history can be cleaned. Rotate any API key that was exposed.

## What Embers does with your data

- Transcripts and the archive stay on your machine. Sources are only read, never modified.
- The offline `stub` summarizer, which is the default, makes no network calls.
- The optional `anthropic` summarizer sends the text being summarized to the Anthropic API using the key in your own environment. It requires an explicit budget cap. Embers does not store or log the key.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository: open the **Security** tab of [hollyvon/embers](https://github.com/hollyvon/embers) and choose **Report a vulnerability**. Please do not open a public issue for a security problem. Include the affected version (`embers --version`), what you expected, what happened, and steps to reproduce using synthetic data only.
