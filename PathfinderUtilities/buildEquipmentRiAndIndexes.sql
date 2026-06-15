USE [PathfinderUtil];
GO

SET NOCOUNT ON;
GO

PRINT 'Building equipment RI and indexes in small steps...';
GO

IF OBJECT_ID(N'pf2.Equipment', N'U') IS NULL
BEGIN
    THROW 50000, 'pf2.Equipment does not exist. Run createEquipmentTables.sql first.', 1;
END
GO

PRINT 'Step 1: Equipment -> Rarity foreign key';
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Equipment_Rarity')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Equipment e
       LEFT JOIN pf2.Rarity r ON r.RarityId = e.RarityId
       WHERE e.RarityId IS NOT NULL
         AND r.RarityId IS NULL
   )
BEGIN
    ALTER TABLE pf2.Equipment WITH CHECK
    ADD CONSTRAINT FK_Equipment_Rarity
    FOREIGN KEY (RarityId) REFERENCES pf2.Rarity(RarityId);

    ALTER TABLE pf2.Equipment CHECK CONSTRAINT FK_Equipment_Rarity;
END
ELSE
BEGIN
    PRINT 'Skipped FK_Equipment_Rarity: already exists or orphaned RarityId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 2: Equipment -> SourceBook foreign key';
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Equipment_SourceBook')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Equipment e
       LEFT JOIN pf2.SourceBook s ON s.SourceBookId = e.SourceBookId
       WHERE e.SourceBookId IS NOT NULL
         AND s.SourceBookId IS NULL
   )
BEGIN
    ALTER TABLE pf2.Equipment WITH CHECK
    ADD CONSTRAINT FK_Equipment_SourceBook
    FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId);

    ALTER TABLE pf2.Equipment CHECK CONSTRAINT FK_Equipment_SourceBook;
END
ELSE
BEGIN
    PRINT 'Skipped FK_Equipment_SourceBook: already exists or orphaned SourceBookId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 3: EquipmentSourceLink -> Equipment foreign key';
IF OBJECT_ID(N'pf2.EquipmentSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_EquipmentSourceLink_Equipment')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.EquipmentSourceLink l
       LEFT JOIN pf2.Equipment e ON e.EquipmentId = l.EquipmentId
       WHERE e.EquipmentId IS NULL
   )
BEGIN
    ALTER TABLE pf2.EquipmentSourceLink WITH CHECK
    ADD CONSTRAINT FK_EquipmentSourceLink_Equipment
    FOREIGN KEY (EquipmentId) REFERENCES pf2.Equipment(EquipmentId);

    ALTER TABLE pf2.EquipmentSourceLink CHECK CONSTRAINT FK_EquipmentSourceLink_Equipment;
END
ELSE
BEGIN
    PRINT 'Skipped FK_EquipmentSourceLink_Equipment: table missing, already exists, or orphaned EquipmentId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 4: EquipmentSourceLink -> SourceBook foreign key';
IF OBJECT_ID(N'pf2.EquipmentSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_EquipmentSourceLink_SourceBook')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.EquipmentSourceLink l
       LEFT JOIN pf2.SourceBook s ON s.SourceBookId = l.SourceBookId
       WHERE s.SourceBookId IS NULL
   )
BEGIN
    ALTER TABLE pf2.EquipmentSourceLink WITH CHECK
    ADD CONSTRAINT FK_EquipmentSourceLink_SourceBook
    FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId);

    ALTER TABLE pf2.EquipmentSourceLink CHECK CONSTRAINT FK_EquipmentSourceLink_SourceBook;
END
ELSE
BEGIN
    PRINT 'Skipped FK_EquipmentSourceLink_SourceBook: table missing, already exists, or orphaned SourceBookId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 5: EquipmentTrait -> Equipment foreign key';
IF OBJECT_ID(N'pf2.EquipmentTrait', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_EquipmentTrait_Equipment')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.EquipmentTrait et
       LEFT JOIN pf2.Equipment e ON e.EquipmentId = et.EquipmentId
       WHERE e.EquipmentId IS NULL
   )
BEGIN
    ALTER TABLE pf2.EquipmentTrait WITH CHECK
    ADD CONSTRAINT FK_EquipmentTrait_Equipment
    FOREIGN KEY (EquipmentId) REFERENCES pf2.Equipment(EquipmentId);

    ALTER TABLE pf2.EquipmentTrait CHECK CONSTRAINT FK_EquipmentTrait_Equipment;
END
ELSE
BEGIN
    PRINT 'Skipped FK_EquipmentTrait_Equipment: table missing, already exists, or orphaned EquipmentId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 6: EquipmentTrait -> Trait foreign key';
IF OBJECT_ID(N'pf2.EquipmentTrait', N'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_EquipmentTrait_Trait')
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.EquipmentTrait et
       LEFT JOIN pf2.Trait t ON t.TraitId = et.TraitId
       WHERE t.TraitId IS NULL
   )
BEGIN
    ALTER TABLE pf2.EquipmentTrait WITH CHECK
    ADD CONSTRAINT FK_EquipmentTrait_Trait
    FOREIGN KEY (TraitId) REFERENCES pf2.Trait(TraitId);

    ALTER TABLE pf2.EquipmentTrait CHECK CONSTRAINT FK_EquipmentTrait_Trait;
END
ELSE
BEGIN
    PRINT 'Skipped FK_EquipmentTrait_Trait: table missing, already exists, or orphaned TraitId rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 7: EquipmentSourceLink uniqueness index';
IF OBJECT_ID(N'pf2.EquipmentSourceLink', N'U') IS NOT NULL
   AND NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.EquipmentSourceLink')
         AND name = N'UX_EquipmentSourceLink'
   )
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.EquipmentSourceLink
       GROUP BY EquipmentId, SourceBookId, PageNumber
       HAVING COUNT(*) > 1
   )
BEGIN
    CREATE UNIQUE INDEX UX_EquipmentSourceLink
    ON pf2.EquipmentSourceLink(EquipmentId, SourceBookId, PageNumber);
END
ELSE
BEGIN
    PRINT 'Skipped UX_EquipmentSourceLink: table missing, already exists, or duplicate rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 8: Equipment AoN key unique index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Equipment')
         AND name = N'UX_Equipment_AonKey'
   )
   AND NOT EXISTS
   (
       SELECT 1
       FROM pf2.Equipment
       WHERE AonKey IS NOT NULL
       GROUP BY AonKey
       HAVING COUNT(*) > 1
   )
BEGIN
    CREATE UNIQUE INDEX UX_Equipment_AonKey
    ON pf2.Equipment(AonKey)
    WHERE AonKey IS NOT NULL;
END
ELSE
BEGIN
    PRINT 'Skipped UX_Equipment_AonKey: already exists or duplicate AonKey rows exist.';
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 9: Equipment URL index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Equipment')
         AND name = N'IX_Equipment_AonUrl'
   )
BEGIN
    CREATE INDEX IX_Equipment_AonUrl ON pf2.Equipment(AonUrl);
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 10: Equipment name index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Equipment')
         AND name = N'IX_Equipment_Name'
   )
BEGIN
    CREATE INDEX IX_Equipment_Name ON pf2.Equipment(Name);
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 11: Equipment category/level index';
IF NOT EXISTS
   (
       SELECT 1
       FROM sys.indexes
       WHERE object_id = OBJECT_ID(N'pf2.Equipment')
         AND name = N'IX_Equipment_Category_Level'
   )
BEGIN
    CREATE INDEX IX_Equipment_Category_Level
    ON pf2.Equipment(SearchCategory, ItemCategory, Level, Name);
END
GO

WAITFOR DELAY '00:00:02';
GO

PRINT 'Step 12: Equipment full-text catalog';
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

PRINT 'Step 13: Equipment full-text index';
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
       WHERE object_id = OBJECT_ID(N'pf2.Equipment')
   )
BEGIN
    CREATE FULLTEXT INDEX ON pf2.Equipment
    (
        Name LANGUAGE 1033,
        Summary LANGUAGE 1033,
        RawText LANGUAGE 1033
    )
    KEY INDEX PK_Equipment
    ON PF2FullTextCatalog
    WITH CHANGE_TRACKING AUTO;
END
ELSE
BEGIN
    PRINT 'Skipped equipment full-text index: Full-Text Search missing, catalog missing, or index already exists.';
END
GO

PRINT 'Finished building equipment RI and indexes.';
GO
