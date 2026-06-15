USE [PathfinderUtil];
GO

SET NOCOUNT ON;
GO

PRINT 'Building feat RI and indexes in small steps...';
GO

IF OBJECT_ID(N'pf2.Feat', N'U') IS NULL
BEGIN
    THROW 50000, 'pf2.Feat does not exist. Run createFeatTables.sql first.', 1;
END
GO

PRINT 'Step 1: Feat -> Rarity foreign key';
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Feat_Rarity')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Feat f
       LEFT JOIN pf2.Rarity r ON r.RarityId = f.RarityId
       WHERE f.RarityId IS NOT NULL
         AND r.RarityId IS NULL
   )
BEGIN
    ALTER TABLE pf2.Feat WITH CHECK
    ADD CONSTRAINT FK_Feat_Rarity
    FOREIGN KEY (RarityId) REFERENCES pf2.Rarity(RarityId);

    ALTER TABLE pf2.Feat CHECK CONSTRAINT FK_Feat_Rarity;
END
ELSE
BEGIN
    PRINT 'Skipped FK_Feat_Rarity: already exists or orphaned RarityId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 2: Feat -> SourceBook foreign key';
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Feat_SourceBook')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Feat f
       LEFT JOIN pf2.SourceBook s ON s.SourceBookId = f.SourceBookId
       WHERE f.SourceBookId IS NOT NULL
         AND s.SourceBookId IS NULL
   )
BEGIN
    ALTER TABLE pf2.Feat WITH CHECK
    ADD CONSTRAINT FK_Feat_SourceBook
    FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId);

    ALTER TABLE pf2.Feat CHECK CONSTRAINT FK_Feat_SourceBook;
END
ELSE
BEGIN
    PRINT 'Skipped FK_Feat_SourceBook: already exists or orphaned SourceBookId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 3: FeatSourceLink -> Feat foreign key';
IF OBJECT_ID(N'pf2.FeatSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_FeatSourceLink_Feat')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.FeatSourceLink l
       LEFT JOIN pf2.Feat f ON f.FeatId = l.FeatId
       WHERE f.FeatId IS NULL
   )
BEGIN
    ALTER TABLE pf2.FeatSourceLink WITH CHECK
    ADD CONSTRAINT FK_FeatSourceLink_Feat
    FOREIGN KEY (FeatId) REFERENCES pf2.Feat(FeatId);

    ALTER TABLE pf2.FeatSourceLink CHECK CONSTRAINT FK_FeatSourceLink_Feat;
END
ELSE
BEGIN
    PRINT 'Skipped FK_FeatSourceLink_Feat: table missing, already exists, or orphaned FeatId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 4: FeatSourceLink -> SourceBook foreign key';
IF OBJECT_ID(N'pf2.FeatSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_FeatSourceLink_SourceBook')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.FeatSourceLink l
       LEFT JOIN pf2.SourceBook s ON s.SourceBookId = l.SourceBookId
       WHERE s.SourceBookId IS NULL
   )
BEGIN
    ALTER TABLE pf2.FeatSourceLink WITH CHECK
    ADD CONSTRAINT FK_FeatSourceLink_SourceBook
    FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId);

    ALTER TABLE pf2.FeatSourceLink CHECK CONSTRAINT FK_FeatSourceLink_SourceBook;
END
ELSE
BEGIN
    PRINT 'Skipped FK_FeatSourceLink_SourceBook: table missing, already exists, or orphaned SourceBookId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 5: FeatTrait -> Feat foreign key';
IF OBJECT_ID(N'pf2.FeatTrait', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_FeatTrait_Feat')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.FeatTrait ft
       LEFT JOIN pf2.Feat f ON f.FeatId = ft.FeatId
       WHERE f.FeatId IS NULL
   )
BEGIN
    ALTER TABLE pf2.FeatTrait WITH CHECK
    ADD CONSTRAINT FK_FeatTrait_Feat
    FOREIGN KEY (FeatId) REFERENCES pf2.Feat(FeatId);

    ALTER TABLE pf2.FeatTrait CHECK CONSTRAINT FK_FeatTrait_Feat;
END
ELSE
BEGIN
    PRINT 'Skipped FK_FeatTrait_Feat: table missing, already exists, or orphaned FeatId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 6: FeatTrait -> Trait foreign key';
IF OBJECT_ID(N'pf2.FeatTrait', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_FeatTrait_Trait')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.FeatTrait ft
       LEFT JOIN pf2.Trait t ON t.TraitId = ft.TraitId
       WHERE t.TraitId IS NULL
   )
BEGIN
    ALTER TABLE pf2.FeatTrait WITH CHECK
    ADD CONSTRAINT FK_FeatTrait_Trait
    FOREIGN KEY (TraitId) REFERENCES pf2.Trait(TraitId);

    ALTER TABLE pf2.FeatTrait CHECK CONSTRAINT FK_FeatTrait_Trait;
END
ELSE
BEGIN
    PRINT 'Skipped FK_FeatTrait_Trait: table missing, already exists, or orphaned TraitId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 7: FeatSourceLink uniqueness index';
IF OBJECT_ID(N'pf2.FeatSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.FeatSourceLink')
         AND name = N'UX_FeatSourceLink'
   )
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.FeatSourceLink
       GROUP BY FeatId, SourceBookId, PageNumber
       HAVING COUNT(*) > 1
   )
BEGIN
    CREATE UNIQUE INDEX UX_FeatSourceLink
    ON pf2.FeatSourceLink(FeatId, SourceBookId, PageNumber);
END
ELSE
BEGIN
    PRINT 'Skipped UX_FeatSourceLink: table missing, already exists, or duplicate rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 8: Feat AoN unique index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Feat')
         AND name = N'UX_Feat_AonId'
   )
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Feat
       WHERE AonId IS NOT NULL
       GROUP BY AonId
       HAVING COUNT(*) > 1
   )
BEGIN
    CREATE UNIQUE INDEX UX_Feat_AonId
    ON pf2.Feat(AonId)
    WHERE AonId IS NOT NULL;
END
ELSE
BEGIN
    PRINT 'Skipped UX_Feat_AonId: already exists or duplicate AonId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 9: Feat name index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Feat')
         AND name = N'IX_Feat_Name'
   )
BEGIN
    CREATE INDEX IX_Feat_Name ON pf2.Feat(Name);
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 10: Feat level/type index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Feat')
         AND name = N'IX_Feat_Level_Type'
   )
BEGIN
    CREATE INDEX IX_Feat_Level_Type ON pf2.Feat(Level, FeatType, Name);
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 11: Feat full-text catalog';
IF FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') = 1
   AND NOT EXISTS
   (
       SELECT 1
       FROM sys.fulltext_catalogs
       WHERE name = N'PF2FullTextCatalog'
   )
BEGIN
    CREATE FULLTEXT CATALOG PF2FullTextCatalog
    WITH ACCENT_SENSITIVITY = OFF;
END
ELSE
BEGIN
    PRINT 'Skipped full-text catalog: Full-Text Search is not installed or catalog already exists.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 12: Feat full-text index';
IF FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') = 1
   AND EXISTS
   (
       SELECT 1
       FROM sys.fulltext_catalogs
       WHERE name = N'PF2FullTextCatalog'
   )
   AND NOT EXISTS
   (
       SELECT 1
       FROM sys.fulltext_indexes
       WHERE object_id = OBJECT_ID(N'pf2.Feat')
   )
BEGIN
    CREATE FULLTEXT INDEX ON pf2.Feat
    (
        Name LANGUAGE 1033,
        Summary LANGUAGE 1033,
        RawText LANGUAGE 1033
    )
    KEY INDEX PK_Feat
    ON PF2FullTextCatalog
    WITH CHANGE_TRACKING AUTO;
END
ELSE
BEGIN
    PRINT 'Skipped feat full-text index: Full-Text Search missing, catalog missing, or index already exists.';
END
GO

PRINT 'Finished building feat RI and indexes.';
GO
