# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **A chat's file is chosen by the chat, not by its display name** (mycelium-hq/tickets#6939).
  - A chat with no known name is no longer filed under its phone number, email or chat id, which apps that show the file name as the title listed as bare numbers. It is `Chat with an unknown number (iMessage) <id>.md` (or `unknown email address`), and `Groups/Group chat (iMessage) <id>.md` for a group with no name. `<id>` is the start of a hash of the handle or chat guid: never the number, the same on every run. A group titled from its members shows only names found in Contacts plus `+ N more`. The handle stays in the frontmatter.
  - Two chats never share a file. Two spellings of one email address are one chat's file. Two groups (or two people) whose names match, ignoring capitals, each get the id added. Ids are computed separately for direct chats and groups, so exporting groups or not never renames a direct chat.
  - Existing files are found by their frontmatter `chat_guid(s)`, not by name: a file named after a number is renamed on the next export, and a chat whose person already has a file is written into that file. The leftover file is removed only when the new file holds at least as many of its messages; otherwise it is kept. Paths are compared as files, not strings: on a case-insensitive disk a rename that only changes capitals keeps the file and renames it, and a name that matches another file ignoring capitals (a note, or a chat outside this run) gets the chat's id instead of overwriting it.
