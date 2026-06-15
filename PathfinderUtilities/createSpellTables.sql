USE [PathfinderUtil];
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'pf2')
BEGIN
    EXEC(N'CREATE SCHEMA pf2');
END
GO

IF OBJECT_ID(N'pf2.Rarity', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Rarity
    (
        RarityId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Rarity PRIMARY KEY,
        Name NVARCHAR(100) NOT NULL CONSTRAINT UQ_Rarity_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.SourceBook', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SourceBook
    (
        SourceBookId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SourceBook PRIMARY KEY,
        Name NVARCHAR(250) NOT NULL CONSTRAINT UQ_SourceBook_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.Trait', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Trait
    (
        TraitId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Trait PRIMARY KEY,
        Name NVARCHAR(150) NOT NULL CONSTRAINT UQ_Trait_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.Tradition', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Tradition
    (
        TraditionId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Tradition PRIMARY KEY,
        Name NVARCHAR(100) NOT NULL CONSTRAINT UQ_Tradition_Name UNIQUE
    );
END
GO

IF OBJECT_ID(N'pf2.Spell', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.Spell
    (
        SpellId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Spell PRIMARY KEY,
        AonId INT NULL,
        AonUrl NVARCHAR(500) NULL,
        Name NVARCHAR(300) NOT NULL,
        Rank INT NULL,
        SpellType NVARCHAR(100) NULL,
        RarityId INT NULL,
        SourceBookId INT NULL,
        SourcePage INT NULL,
        Actions NVARCHAR(100) NULL,
        TriggerText NVARCHAR(MAX) NULL,
        Target NVARCHAR(MAX) NULL,
        RangeText NVARCHAR(250) NULL,
        Area NVARCHAR(250) NULL,
        Duration NVARCHAR(250) NULL,
        Defense NVARCHAR(300) NULL,
        Heighten NVARCHAR(MAX) NULL,
        Summary NVARCHAR(MAX) NULL,
        PFS NVARCHAR(100) NULL,
        Components NVARCHAR(500) NULL,
        School NVARCHAR(150) NULL,
        Bloodline NVARCHAR(500) NULL,
        DomainText NVARCHAR(500) NULL,
        RemasterId NVARCHAR(500) NULL,
        RemasterName NVARCHAR(500) NULL,
        RawHtml NVARCHAR(MAX) NULL,
        RawText NVARCHAR(MAX) NULL,
        RawJson NVARCHAR(MAX) NULL,
        CreatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Spell_CreatedAt DEFAULT SYSDATETIME(),
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Spell_UpdatedAt DEFAULT SYSDATETIME(),
        LastScraped DATETIME2(7) NOT NULL CONSTRAINT DF_Spell_LastScraped DEFAULT SYSDATETIME(),
        ScrapeVersion NVARCHAR(100) NULL,
        CONSTRAINT FK_Spell_Rarity FOREIGN KEY (RarityId) REFERENCES pf2.Rarity(RarityId),
        CONSTRAINT FK_Spell_SourceBook FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId)
    );

    CREATE UNIQUE INDEX UX_Spell_AonId
        ON pf2.Spell(AonId)
        WHERE AonId IS NOT NULL;

    CREATE INDEX IX_Spell_Name ON pf2.Spell(Name);
    CREATE INDEX IX_Spell_Rank_Type ON pf2.Spell(Rank, SpellType, Name);
END
GO

IF OBJECT_ID(N'pf2.SpellSourceLink', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SpellSourceLink
    (
        SpellSourceLinkId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SpellSourceLink PRIMARY KEY,
        SpellId INT NOT NULL,
        SourceBookId INT NOT NULL,
        PageNumber INT NULL,
        CONSTRAINT FK_SpellSourceLink_Spell FOREIGN KEY (SpellId) REFERENCES pf2.Spell(SpellId),
        CONSTRAINT FK_SpellSourceLink_SourceBook FOREIGN KEY (SourceBookId) REFERENCES pf2.SourceBook(SourceBookId),
        CONSTRAINT UQ_SpellSourceLink UNIQUE (SpellId, SourceBookId, PageNumber)
    );
END
GO

IF OBJECT_ID(N'pf2.SpellTrait', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SpellTrait
    (
        SpellId INT NOT NULL,
        TraitId INT NOT NULL,
        CONSTRAINT PK_SpellTrait PRIMARY KEY (SpellId, TraitId),
        CONSTRAINT FK_SpellTrait_Spell FOREIGN KEY (SpellId) REFERENCES pf2.Spell(SpellId),
        CONSTRAINT FK_SpellTrait_Trait FOREIGN KEY (TraitId) REFERENCES pf2.Trait(TraitId)
    );
END
GO

IF OBJECT_ID(N'pf2.SpellTradition', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SpellTradition
    (
        SpellId INT NOT NULL,
        TraditionId INT NOT NULL,
        CONSTRAINT PK_SpellTradition PRIMARY KEY (SpellId, TraditionId),
        CONSTRAINT FK_SpellTradition_Spell FOREIGN KEY (SpellId) REFERENCES pf2.Spell(SpellId),
        CONSTRAINT FK_SpellTradition_Tradition FOREIGN KEY (TraditionId) REFERENCES pf2.Tradition(TraditionId)
    );
END
GO

IF OBJECT_ID(N'pf2.SpellImportLog', N'U') IS NULL
BEGIN
    CREATE TABLE pf2.SpellImportLog
    (
        SpellImportLogId INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SpellImportLog PRIMARY KEY,
        AonUrl NVARCHAR(500) NULL,
        ImportedAt DATETIME2(7) NOT NULL CONSTRAINT DF_SpellImportLog_ImportedAt DEFAULT SYSDATETIME(),
        Success BIT NOT NULL,
        Message NVARCHAR(4000) NULL
    );
END
GO
