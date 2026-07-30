# PostgreSQL migration plan for BestLogMarketPlaceProject

## Goal
Preserve all production marketplace data while moving from SQLite to PostgreSQL without changing business logic, models, views, URLs, templates, admin behavior, or payment integrations.

## Safe migration strategy
Recommended approach: Django-native migration with a backup-first workflow.

1. Create a full backup of the current SQLite database and media files.
2. Provision a PostgreSQL database and a dedicated application role.
3. Configure environment variables for PostgreSQL and production settings.
4. Run Django migrations in PostgreSQL.
5. Export data from SQLite using Django's serializer.
6. Import data into PostgreSQL.
7. Validate counts and relationships.
8. Switch the deployment to PostgreSQL and test the live site.

## Backup steps
- Copy the existing SQLite file: db.sqlite3
- Copy the media directory: media/
- Export a data snapshot: python manage.py dumpdata --natural-foreign --natural-primary > data.json

## PostgreSQL setup
- Create a dedicated application role (not a superuser).
- Create a database owned by that role.
- Grant only the minimum required privileges.

## Migration steps
1. Set environment variables for PostgreSQL.
2. Run: python manage.py migrate
3. Import data: python manage.py loaddata data.json

## Validation steps
- Verify Django checks pass.
- Compare counts for users, products, categories, supplier products, transactions, cart rows, sessions, and admin accounts.
- Verify critical relationships and image/media files.

## Rollback plan
- Keep the SQLite backup intact.
- Do not remove the old database until PostgreSQL has been validated.
- If validation fails, revert the deployment environment variables to SQLite and restore the backup.
